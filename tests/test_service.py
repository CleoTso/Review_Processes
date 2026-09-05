import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_processes.vendor_review.audit import Category, FindingStatus
from review_processes.vendor_review.models import AttachmentRef, Evidence, FieldChange, Proposal
from review_processes.vendor_review.service import VendorReviewService
from review_processes.vendor_review.store import AuditReportStore, ProposalStore


class AirtableFake:
    def __init__(self, records):
        self._records = records
        self.requested_view = None

    def records(self, view=None):
        self.requested_view = view
        return self._records


class GmailFake:
    def __init__(self):
        self.queries = []
        self.mail = {
            "m1": {
                "id": "m1",
                "internalDate": "1787227200000",
                "payload": {"headers": [
                    {"name": "Subject", "value": "Executed Maintenance Agreement"},
                    {"name": "From", "value": "vendor@example.com"},
                ]},
            }
        }

    def search(self, query, max_results=500):
        self.queries.append(query)
        return ["m1"]

    def message(self, message_id):
        return self.mail[message_id]

    def attachment(self, message_id, attachment_id):
        return b""


class BatchGmailFake(GmailFake):
    def __init__(self):
        super().__init__()
        self.batch_calls = []
        self.attachment_calls = []

    def messages(self, message_ids, *, format="full"):
        self.batch_calls.append(list(message_ids))
        self.assert_format = format
        return [self.mail[message_id] for message_id in message_ids]

    def attachment(self, message_id, attachment_id):
        self.attachment_calls.append((message_id, attachment_id))
        return b"should-not-download"


class ForbiddenThenReadableGmailFake(GmailFake):
    def __init__(self):
        super().__init__()
        self.requested_format = None

    def search(self, query, max_results=500):
        self.queries.append(query)
        return ["blocked", "m1"]

    def messages(self, message_ids, *, format="full"):
        self.requested_format = format
        loaded = []
        for message_id in message_ids:
            if message_id == "blocked":
                continue
            loaded.append(self.mail[message_id])
        return loaded


class ServiceTests(unittest.TestCase):
    def test_audit_searches_all_required_document_categories_and_persists_report(self):
        with tempfile.TemporaryDirectory() as directory:
            gmail = GmailFake()
            audit_store = AuditReportStore(Path(directory))
            airtable = AirtableFake([{
                "id": "v1",
                "fields": {"Vendor": "ACME Services", "Contact Email": "vendor@example.com"},
            }])
            service = VendorReviewService(
                airtable,
                gmail,
                ProposalStore(Path(directory)),
                audit_store,
                "viwVD8IFpH6fXUPvh",
            )

            report = service.audit(lookback_days=90, history_days=730)

            self.assertEqual(len(gmail.queries), 3)
            self.assertEqual(airtable.requested_view, "viwVD8IFpH6fXUPvh")
            query = " ".join(gmail.queries).lower()
            self.assertIn("newer_than:730d", query)
            for term in ("contract", "agreement", "terms", "insurance", "policy", "maintenance", "hvac"):
                self.assertIn(term, query)
            self.assertEqual(report.vendors[0].findings[Category.MAINTENANCE].status, FindingStatus.DOCUMENTED_RECENT)
            self.assertEqual(audit_store.load().vendors[0].name, "ACME Services")

    def test_audit_uses_batched_metadata_and_skips_attachment_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            gmail = BatchGmailFake()
            service = VendorReviewService(
                AirtableFake([{
                    "id": "v1",
                    "fields": {"Vendor": "ACME Services", "Contact Email": "vendor@example.com"},
                }]),
                gmail,
                ProposalStore(Path(directory)),
                AuditReportStore(Path(directory)),
                "viwVD8IFpH6fXUPvh",
            )

            report = service.audit(lookback_days=90, history_days=180)

            self.assertEqual(gmail.batch_calls, [["m1"]])
            self.assertEqual(gmail.attachment_calls, [])
            self.assertEqual(report.messages_scanned, 1)

    def test_audit_keeps_readable_messages_when_one_gmail_lookup_is_forbidden(self):
        with tempfile.TemporaryDirectory() as directory:
            gmail = ForbiddenThenReadableGmailFake()
            service = VendorReviewService(
                AirtableFake([{
                    "id": "v1",
                    "fields": {"Vendor": "ACME Services", "Contact Email": "vendor@example.com"},
                }]),
                gmail,
                ProposalStore(Path(directory)),
                AuditReportStore(Path(directory)),
                "viwVD8IFpH6fXUPvh",
            )

            report = service.audit(lookback_days=90, history_days=180)

            self.assertEqual(gmail.requested_format, "full")
            self.assertEqual(report.messages_scanned, 1)
            self.assertEqual(report.vendors[0].findings[Category.MAINTENANCE].status, FindingStatus.DOCUMENTED_RECENT)


class ApplyAirtableFake:
    def __init__(self):
        self.updates = []
        self.uploads = []
        self.records = {"rec1": {"id": "rec1", "fields": {"Vendor": "Old"}}}

    def record(self, record_id):
        return self.records[record_id]

    def update(self, record_id, fields):
        self.updates.append((record_id, dict(fields)))
        self.records[record_id]["fields"].update(fields)
        return self.records[record_id]

    def schema(self):
        return {"fields": [{"name": "Contracts & Warranties", "id": "fld1"}]}

    def upload_attachment(self, record_id, field_id, filename, content_type, data):
        self.uploads.append((record_id, field_id, filename))


class ApplyGmailFake:
    def __init__(self, parts):
        self.parts = parts

    def message(self, message_id):
        return {"id": message_id, "payload": {"parts": self.parts}}

    def attachment(self, message_id, attachment_id):
        return b"attachment-bytes"


def attachment_proposal(filename="contract.pdf"):
    return Proposal(
        id="VR-APPLY-1", kind="test", record_id="rec1", vendor_before="Old", store=None,
        confidence=1.0, changes=[FieldChange("Vendor", "Old", "New", "proof")],
        evidence=[Evidence("m1", "subject", "sender", "date", "url")],
        attachments=[AttachmentRef("m1", filename)],
    )


class ApplyTests(unittest.TestCase):
    def test_apply_uploads_evidence_attachment_and_marks_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            airtable = ApplyAirtableFake()
            gmail = ApplyGmailFake([
                {"filename": "contract.pdf", "body": {"attachmentId": "att1"},
                 "mimeType": "application/pdf"},
            ])
            store = ProposalStore(Path(directory))
            service = VendorReviewService(airtable, gmail, store)
            store.upsert([attachment_proposal()])
            proposal = store.get("VR-APPLY-1")

            result = service.apply(proposal)

            self.assertEqual(airtable.updates, [("rec1", {"Vendor": "New"})])
            self.assertEqual(airtable.uploads, [("rec1", "fld1", "contract.pdf")])
            self.assertEqual(result["attachments"], ["contract.pdf"])
            self.assertEqual(store.get("VR-APPLY-1").status, "applied")

    def test_apply_refuses_when_evidence_attachment_disappeared_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            airtable = ApplyAirtableFake()
            gmail = ApplyGmailFake([])  # Gmail message no longer carries the attachment
            store = ProposalStore(Path(directory))
            service = VendorReviewService(airtable, gmail, store)
            proposal = attachment_proposal()
            store.upsert([proposal])

            with self.assertRaises(RuntimeError) as caught:
                service.apply(service.store.get("VR-APPLY-1"))

            self.assertIn("no longer present", str(caught.exception))
            self.assertIn("Rescan", str(caught.exception))
            # Nothing was written to Airtable and the proposal stays pending.
            self.assertEqual(airtable.updates, [])
            self.assertEqual(airtable.uploads, [])
            self.assertEqual(store.get("VR-APPLY-1").status, "pending")

    def test_dry_run_does_not_write_or_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            airtable = ApplyAirtableFake()
            gmail = ApplyGmailFake([
                {"filename": "contract.pdf", "body": {"attachmentId": "att1"},
                 "mimeType": "application/pdf"},
            ])
            store = ProposalStore(Path(directory))
            service = VendorReviewService(airtable, gmail, store)
            proposal = attachment_proposal()

            result = service.apply(proposal, dry_run=True)

            self.assertEqual(airtable.updates, [])
            self.assertEqual(airtable.uploads, [])
            self.assertEqual(result["fields"], {"Vendor": "New"})


if __name__ == "__main__":
    unittest.main()
