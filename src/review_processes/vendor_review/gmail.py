from __future__ import annotations

from pathlib import Path
from typing import Any

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .mime import attachments, decode, headers, received_at, text_parts, walk_parts


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def authorize(client_secret: Path, token_file: Path) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_file.write_text(credentials.to_json())


class GmailClient:
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, token_file: Path):
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_file.write_text(credentials.to_json())
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

    def message(self, message_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.API}/messages/{message_id}", params={"format": "full"}, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self.session.get(
            f"{self.API}/messages/{message_id}/attachments/{attachment_id}", timeout=60
        )
        response.raise_for_status()
        return decode(response.json()["data"])
