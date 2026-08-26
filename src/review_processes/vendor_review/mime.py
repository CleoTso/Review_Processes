from __future__ import annotations

import base64
from email.utils import parsedate_to_datetime
from typing import Any


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def headers(message: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def received_at(message: dict[str, Any]) -> str:
    value = headers(message).get("date")
    if value:
        try:
            return parsedate_to_datetime(value).isoformat()
        except (TypeError, ValueError):
            pass
    return str(message.get("internalDate", ""))


def text_parts(message: dict[str, Any]) -> str:
    output: list[str] = []
    for part in walk_parts(message.get("payload", {})):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            output.append(decode(data).decode(errors="replace"))
    return "\n".join(output)


def walk_parts(part: dict[str, Any]):
    yield part
    for child in part.get("parts", []) or []:
        yield from walk_parts(child)


def attachments(message: dict[str, Any]) -> list[dict[str, str]]:
    found = []
    for part in walk_parts(message.get("payload", {})):
        attachment_id = part.get("body", {}).get("attachmentId")
        filename = part.get("filename")
        if attachment_id and filename:
            found.append(
                {
                    "id": attachment_id,
                    "filename": filename,
                    "mime_type": part.get("mimeType", "application/octet-stream"),
                }
            )
    return found

