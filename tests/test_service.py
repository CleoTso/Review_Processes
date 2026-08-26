import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_processes.vendor_review.audit import Category, FindingStatus
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


if __name__ == "__main__":
    unittest.main()
