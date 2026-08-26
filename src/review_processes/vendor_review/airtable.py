from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import requests


class AirtableClient:
    API = "https://api.airtable.com/v0"
    CONTENT_API = "https://content.airtable.com/v0"

    def __init__(self, token: str, base_id: str, table: str):
        self.base_id = base_id
        self.table = table
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    @property
    def table_url(self) -> str:
        return f"{self.API}/{self.base_id}/{quote(self.table, safe='')}"

    def records(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": 100}
        while True:
            response = self.session.get(self.table_url, params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            output.extend(body["records"])
            if not body.get("offset"):
                return output
            params["offset"] = body["offset"]

    def record(self, record_id: str) -> dict[str, Any]:
        response = self.session.get(f"{self.table_url}/{record_id}", timeout=30)
        response.raise_for_status()
        return response.json()

    def update(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(
            f"{self.table_url}/{record_id}", json={"fields": fields}, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def upload_attachment(
        self, record_id: str, field_id: str, filename: str, content_type: str, data: bytes
    ) -> dict[str, Any]:
        url = f"{self.CONTENT_API}/{self.base_id}/{record_id}/{field_id}/uploadAttachment"
        response = self.session.post(
            url,
            json={
                "contentType": content_type,
                "filename": filename,
                "file": base64.b64encode(data).decode(),
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def schema(self) -> dict[str, Any]:
        response = self.session.get(
            f"https://api.airtable.com/v0/meta/bases/{self.base_id}/tables", timeout=30
        )
        response.raise_for_status()
        tables = response.json()["tables"]
        return next(table for table in tables if table["name"] == self.table)

