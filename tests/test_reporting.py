import unittest
from datetime import date

from review_processes.vendor_review.models import Evidence, FieldChange, Proposal
from review_processes.vendor_review.reporting import AuditCompletion, publish_completion, render_notion_entry


def sample_proposal():
    return Proposal(
        id="VR-20260826-ABC123",
        kind="provider_transition",
        record_id="rec1",
        vendor_before="Old Utility",
        store="RoundRock",
        confidence=0.99,
        changes=[FieldChange("Vendor", "Old Utility", "New Utility", "Executed agreement")],
        evidence=[Evidence("m1", "Enrollment", "support@example.com", "2026-08-26", "https://mail")],
    )


class ReportingTests(unittest.TestCase):
    def test_notion_entry_starts_with_dated_heading(self):
        entry = render_notion_entry(AuditCompletion(date(2026, 8, 26), [sample_proposal()]))
        self.assertTrue(entry.startswith("## Vendor Audit — August 26, 2026"))
        self.assertIn("VR-20260826-ABC123", entry)
        self.assertIn("Airtable remained unchanged", entry)

    def test_notion_is_written_before_slack(self):
        calls = []
        publish_completion(
            AuditCompletion(date(2026, 8, 26), [sample_proposal()]),
            lambda text: calls.append(("notion", text)),
            lambda text: calls.append(("slack", text)),
        )
        self.assertEqual([name for name, _ in calls], ["notion", "slack"])

    def test_slack_is_not_sent_when_notion_fails(self):
        calls = []

        def fail(_):
            calls.append("notion")
            raise RuntimeError("Notion unavailable")

        with self.assertRaises(RuntimeError):
            publish_completion(
                AuditCompletion(date(2026, 8, 26)),
                fail,
                lambda _: calls.append("slack"),
            )
        self.assertEqual(calls, ["notion"])


if __name__ == "__main__":
    unittest.main()

