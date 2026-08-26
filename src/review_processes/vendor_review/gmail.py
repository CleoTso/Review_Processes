from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .mime import decode


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_METADATA_HEADERS = [
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "Reply-To",
    "X-Original-Sender",
]


def authorize(client_secret: Path, token_file: Path) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    credentials = flow.run_local_server(port=0)
    _write_private_token(token_file, credentials.to_json())


class GmailClient:
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, token_file: Path):
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _write_private_token(token_file, credentials.to_json())
        self.session = AuthorizedSession(credentials)

    def search(self, query: str, max_results: int = 500) -> list[str]:
        ids: list[str] = []
        params: dict[str, Any] = {"q": query, "maxResults": min(max_results, 500)}
        while len(ids) < max_results:
            response = self.session.get(f"{self.API}/messages", params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            ids.extend(item["id"] for item in body.get("messages", []))
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return ids[:max_results]

    def messages(self, message_ids: list[str], *, format: str = "metadata") -> list[dict[str, Any]]:
        """Fetch many Gmail messages, preserving input order."""
        if not message_ids:
            return []
        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(message_ids), 50):
            chunk = message_ids[start : start + 50]
            results.update(self._batch_get(chunk, format=format))
        missing = [message_id for message_id in message_ids if message_id not in results]
        for message_id in missing:
            results[message_id] = self.message(message_id, format=format)
        return [results[message_id] for message_id in message_ids]

    def message(self, message_id: str, *, format: str = "full") -> dict[str, Any]:
        params: dict[str, Any] = {"format": format}
        if format == "metadata":
            params["metadataHeaders"] = _METADATA_HEADERS
        response = self.session.get(
            f"{self.API}/messages/{message_id}", params=params, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self.session.get(
            f"{self.API}/messages/{message_id}/attachments/{attachment_id}", timeout=60
        )
        response.raise_for_status()
        return decode(response.json()["data"])

    def _batch_get(self, message_ids: list[str], *, format: str) -> dict[str, dict[str, Any]]:
        boundary = "batch_vendor_audit"
        query = urlencode({"format": format}, doseq=True)
        if format == "metadata":
            query += "".join(f"&metadataHeaders={header}" for header in _METADATA_HEADERS)
        body_parts = []
        for index, message_id in enumerate(message_ids):
            body_parts.append(
                "\r\n".join(
                    [
                        f"--{boundary}",
                        "Content-Type: application/http",
                        f"Content-ID: <item{index}>",
                        "",
                        f"GET /gmail/v1/users/me/messages/{message_id}?{query} HTTP/1.1",
                        "",
                    ]
                )
            )
        payload = ("\r\n".join(body_parts) + f"\r\n--{boundary}--\r\n").encode("utf-8")
        response = self.session.post(
            "https://gmail.googleapis.com/batch/gmail/v1",
            data=payload,
            headers={"Content-Type": f"multipart/mixed; boundary={boundary}"},
            timeout=60,
        )
        if response.status_code >= 400:
            return {}
        return _parse_batch_messages(response.text)


def _write_private_token(token_file: Path, content: str) -> None:
    """Atomically persist OAuth material so it is never group/world-readable."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{token_file.name}.", dir=token_file.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, token_file)
        os.chmod(token_file, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _parse_batch_messages(body: str) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = body.find("{", index)
        if start == -1:
            break
        try:
            payload, end = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        if isinstance(payload, dict):
            message_id = payload.get("id")
            if isinstance(message_id, str) and "payload" in payload:
                results[message_id] = payload
    return results
