from __future__ import annotations

from typing import Any

from .airtable import AirtableClient
from .audit import AuditReport, review_vendors
from .detect import detect
from .gmail import GmailClient
from .mime import attachments
from .models import Proposal
from .store import AuditReportStore, ProposalStore


class VendorReviewService:
    def __init__(
        self,
        airtable: AirtableClient,
        gmail: GmailClient,
        store: ProposalStore,
        audit_store: AuditReportStore | None = None,
        airtable_view: str | None = None,
    ):
        self.airtable = airtable
        self.gmail = gmail
        self.store = store
        self.audit_store = audit_store or AuditReportStore(store.state_dir)
        self.airtable_view = airtable_view

    def audit(self, lookback_days: int, history_days: int | None = None) -> AuditReport:
        """Build and persist the complete read-only vendor review matrix."""
        history_days = max(history_days or max(lookback_days, 730), lookback_days)
        vendors = self.airtable.records(view=self.airtable_view)
        messages = self._load_messages(history_days)
        report = review_vendors(
            vendors,
            messages,
            lookback_days,
            history_days=history_days,
        )
        self.audit_store.save(report)
        return report

    def _load_messages(self, lookback_days: int):
        # Keep discovery separate by control.  Gmail's search parser can turn a
        # large mixed OR expression into a noisy result set (or omit a narrow
        # branch), which is unacceptable for insurance/COI coverage checks.
        queries = [
            f"newer_than:{lookback_days}d (contract OR agreement OR terms OR renewal OR amendment OR addendum OR \"rate change\") -in:spam -in:trash",
            f"newer_than:{lookback_days}d (insurance OR COI OR liability OR \"additional insured\" OR \"workers compensation\" OR \"insurance policy\") -in:spam -in:trash",
            f"newer_than:{lookback_days}d (maintenance OR HVAC OR refrigeration OR plumbing OR pest OR hood OR grease OR repair OR \"service agreement\") -in:spam -in:trash",
        ]
        message_ids: list[str] = []
        seen: set[str] = set()
        for query in queries:
            for message_id in self.gmail.search(query, max_results=2000):
                if message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
        if hasattr(self.gmail, "messages"):
            loaded = self.gmail.messages(message_ids, format="full")
        else:
            loaded = []
            for message_id in message_ids:
                try:
                    loaded.append(self.gmail.message(message_id))
                except Exception:
                    continue
        return [(message, {}) for message in loaded]

    def scan(self, lookback_days: int, required_store: str | None = None) -> list[Proposal]:
        vendors = self.airtable.records(view=self.airtable_view)
        query = (
            f"newer_than:{lookback_days}d "
            "(invoice OR bill OR contract OR agreement OR enrollment OR terms OR renewal) "
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
        pending_uploads = []
        if proposal.attachments:
            schema = self.airtable.schema()
            field_ids = {field["name"]: field["id"] for field in schema["fields"]}
            refreshed = self.airtable.record(proposal.record_id)["fields"]
            for ref in proposal.attachments:
                existing = refreshed.get(ref.airtable_field, []) or []
                if any(x.get("filename") == ref.filename for x in existing):
                    continue
                message = self.gmail.message(ref.message_id)
                item = next(
                    (x for x in attachments(message) if x["filename"] == ref.filename), None
                )
                if item is None:
                    raise RuntimeError(
                        f"Evidence attachment {ref.filename} is no longer present on Gmail "
                        f"message {ref.message_id}; nothing was written to Airtable. "
                        "Rescan and approve a fresh proposal."
                    )
                pending_uploads.append((ref, field_ids[ref.airtable_field], item))
        self.airtable.update(proposal.record_id, updates)
        for ref, field_id, item in pending_uploads:
            data = self.gmail.attachment(ref.message_id, item["id"])
            self.airtable.upload_attachment(
                proposal.record_id,
                field_id,
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
