from __future__ import annotations

from typing import Any

from .airtable import AirtableClient
from .detect import detect
from .gmail import GmailClient
from .mime import attachments
from .models import Proposal
from .store import ProposalStore


class VendorReviewService:
    def __init__(self, airtable: AirtableClient, gmail: GmailClient, store: ProposalStore):
        self.airtable = airtable
        self.gmail = gmail
        self.store = store

    def scan(self, lookback_days: int, required_store: str | None = None) -> list[Proposal]:
        vendors = self.airtable.records()
        query = (
            f"newer_than:{lookback_days}d "
            "(invoice OR bill OR contract OR agreement OR enrollment) "
            "-in:spam -in:trash -category:promotions"
        )
        messages = []
        for message_id in self.gmail.search(query):
            message = self.gmail.message(message_id)
            blobs: dict[str, bytes] = {}
            for item in attachments(message):
                if item["filename"].lower().endswith(".pdf"):
                    blobs[item["filename"]] = self.gmail.attachment(message_id, item["id"])
            messages.append((message, blobs))
        proposals = self.store.upsert(detect(vendors, messages))
        pending = [p for p in proposals if p.status == "pending"]
        if required_store and not any(
            p.store and p.store.lower() == required_store.lower() for p in pending
        ):
            raise RuntimeError(f"No pending evidence-backed proposal found for required store {required_store}")
        return pending

    def apply(self, proposal: Proposal, dry_run: bool = False) -> dict[str, Any]:
        unanswered = [q.prompt for q in proposal.questions if q.required and not q.answer]
        if unanswered:
            raise RuntimeError("Required questions remain: " + "; ".join(unanswered))
        current = self.airtable.record(proposal.record_id)
        fields = current["fields"]
        updates = {change.field_name: change.after for change in proposal.changes}
        self._add_answers(proposal, updates)
        drift = [
            change.field_name
            for change in proposal.changes
            if fields.get(change.field_name) not in (change.before, change.after)
        ]
        if drift:
            raise RuntimeError("Airtable changed since scan; rescan before approval: " + ", ".join(drift))
        result = {"record_id": proposal.record_id, "fields": updates, "attachments": []}
        if dry_run:
            return result
        self.airtable.update(proposal.record_id, updates)
        if proposal.attachments:
            schema = self.airtable.schema()
            field_ids = {field["name"]: field["id"] for field in schema["fields"]}
            refreshed = self.airtable.record(proposal.record_id)["fields"]
            for ref in proposal.attachments:
                existing = refreshed.get(ref.airtable_field, []) or []
                if any(x.get("filename") == ref.filename for x in existing):
                    continue
                message = self.gmail.message(ref.message_id)
                item = next(x for x in attachments(message) if x["filename"] == ref.filename)
                data = self.gmail.attachment(ref.message_id, item["id"])
                self.airtable.upload_attachment(
                    proposal.record_id,
                    field_ids[ref.airtable_field],
                    ref.filename,
                    item["mime_type"],
                    data,
                )
                result["attachments"].append(ref.filename)
        proposal.status = "applied"
        self.store.replace(proposal)
        return result

    @staticmethod
    def _add_answers(proposal: Proposal, updates: dict[str, Any]) -> None:
        answers = {q.key: q.answer for q in proposal.questions if q.answer}
        if answers.get("portal_url"):
            updates["Website"] = answers["portal_url"]
        if answers.get("billing_account"):
            updates["Account #"] = answers["billing_account"]
        if answers.get("payment_method"):
            updates["Payment Method"] = answers["payment_method"]
