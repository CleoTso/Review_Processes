import unittest
from datetime import datetime, timezone

from review_processes.vendor_review.audit import (
    Category,
    FindingStatus,
    classify_document,
    review_vendors,
)


def message(message_id, subject, body, sender="vendor@example.com", received="2026-08-20T12:00:00+00:00"):
    return (
        {
            "id": message_id,
            "internalDate": str(int(datetime.fromisoformat(received).timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                    {"name": "Date", "value": "Thu, 20 Aug 2026 12:00:00 +0000"},
                ]
            },
        },
        {},
        body,
    )


class ClassificationTests(unittest.TestCase):
    def test_classifies_each_review_category(self):
        self.assertEqual(
            classify_document("Executed Service Agreement", "contract term renewal", ["agreement.pdf"]),
            {Category.CONTRACT_TERMS},
        )
        self.assertEqual(
            classify_document("Certificate of Insurance", "additional insured general liability", ["COI.pdf"]),
            {Category.INSURANCE},
        )
        self.assertEqual(
            classify_document("Preventive Maintenance Agreement", "HVAC service schedule", ["maintenance.pdf"]),
            {Category.MAINTENANCE},
        )

    def test_invoice_alone_is_not_a_contract_finding(self):
        self.assertEqual(classify_document("Invoice #123", "monthly invoice amount due", ["invoice.pdf"]), set())

    def test_body_only_contract_word_is_not_enough(self):
        self.assertEqual(classify_document("Password verification code", "Your contract code is 1234", []), set())

    def test_legal_footer_link_is_not_updated_terms_evidence(self):
        footer = '<a href="https://vendor.example/legal/terms">Terms of service</a> | <a href="https://vendor.example/privacy">Privacy</a>'
        self.assertEqual(classify_document("Password verification code", footer, []), set())

    def test_attached_insurance_quote_is_not_issued_coverage(self):
        self.assertEqual(
            classify_document("Insurance quote for Tso", "We offer a quote; please reply for coverage", ["insurance-quote.docx"]),
            {Category.INSURANCE},
        )
        msg = message("m1", "Insurance quote for Tso", "We offer a quote; please reply for coverage", "sales@broker.example.com")
        report = review_vendors(
            [{"id": "v1", "fields": {"Name": "Broker", "Email 1": "sales@broker.example.com"}}],
            [(msg[0], {"insurance-quote.docx": b"not-a-real-docx"}, msg[2])],
            lookback_days=90,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(report.vendors[0].findings[Category.INSURANCE].status, FindingStatus.POSSIBLE_LEAD)


class ReviewTests(unittest.TestCase):
    def test_solicitations_are_leads_not_proof(self):
        vendors = [{"id": "v1", "fields": {"Name": "Ned Air Filters"}}]
        messages = [message("m1", "AC Filter Maintenance for Tso Chinese", "We offer scheduled reminders and delivery. Get a quote.", "sales@nedairfilters.com")]
        report = review_vendors(vendors, messages, lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        review = report.vendors[0]
        self.assertEqual(review.findings[Category.MAINTENANCE].status, FindingStatus.POSSIBLE_LEAD)
        self.assertEqual(review.findings[Category.INSURANCE].status, FindingStatus.MISSING)

    def test_missing_categories_are_reported_for_every_vendor(self):
        vendors = [{"id": "v1", "fields": {"Name": "ACME Services"}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(set(report.vendors[0].findings), set(Category))
        self.assertTrue(all(f.status == FindingStatus.MISSING for f in report.vendors[0].findings.values()))

    def test_recent_and_old_evidence_are_distinguished(self):
        vendors = [{"id": "v1", "fields": {"Name": "ACME Services", "Email 1": "vendor@example.com"}}]
        messages = [
            message("old", "Service Agreement", "executed agreement term", received="2025-01-01T12:00:00+00:00"),
            message("new", "Service Agreement", "executed agreement term", received="2026-08-01T12:00:00+00:00"),
        ]
        report = review_vendors(vendors, messages, lookback_days=90, history_days=730, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        finding = report.vendors[0].findings[Category.CONTRACT_TERMS]
        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
        self.assertEqual([e.message_id for e in finding.evidence], ["new", "old"])

    def test_group_forwarded_original_sender_matches_vendor(self):
        vendors = [{"id": "v1", "fields": {"Name": "Cool HVAC", "Email 1": "service@coolhvac.com"}}]
        msg, blobs, body = message("m1", "Maintenance Agreement", "executed maintenance agreement", "catering@tsochinese.com")
        msg["payload"]["headers"].append({"name": "X-Original-Sender", "value": "service@coolhvac.com"})
        report = review_vendors(vendors, [(msg, blobs, body)], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(report.vendors[0].findings[Category.MAINTENANCE].status, FindingStatus.DOCUMENTED_RECENT)

    def test_uncatalogued_candidate_is_retained(self):
        msg = message("m1", "Executed Service Agreement", "executed contract agreement", "contracts@newvendor.com")
        report = review_vendors([], [msg], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(len(report.uncatalogued), 1)
        self.assertEqual(report.uncatalogued[0].name, "newvendor.com")

    def test_incomplete_gmail_header_does_not_abort_review(self):
        msg = message("m1", "Executed Service Agreement", "executed contract agreement", "contracts@newvendor.com")[0]
        msg["payload"]["headers"].append({"name": "X-Malformed"})
        report = review_vendors([{"id": "v1", "fields": {"Name": "newvendor.com"}}], [(msg, {}, "executed contract agreement")], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(report.messages_scanned, 1)

    def test_offboarded_vendor_is_visible_but_not_active(self):
        vendors = [{"id": "v1", "fields": {"Name": "Former Vendor", "Status": ["Off Boarded"]}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertFalse(report.vendors[0].active)
        self.assertEqual(report.active_directory_count, 0)

    def test_current_status_is_used_for_active_directory_count(self):
        vendors = [{"id": "v1", "fields": {"Vendor": "Former Vendor", "Current Status": ["Inactive"]}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertFalse(report.vendors[0].active)
        self.assertEqual(report.active_directory_count, 0)


if __name__ == "__main__":
    unittest.main()
