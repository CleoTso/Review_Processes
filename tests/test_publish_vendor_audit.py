import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from publish_vendor_audit import deliver_slack, render_report  # pyright: ignore[reportMissingImports]


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.body).encode()


class FakeOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if request.full_url.endswith("conversations.open"):
            return FakeResponse({"ok": True, "channel": {"id": "DTEST"}})
        return FakeResponse({"ok": True, "ts": "test"})


def sample_report():
    return {
        "generated_at": "2026-08-26T15:00:00+00:00",
        "directory_count": 1,
        "active_directory_count": 1,
        "messages_scanned": 4,
        "missing_count": 1,
        "vendors": [
            {
                "name": "Example Vendor",
                "active": True,
                "findings": {
                    "contract_terms": {"status": "documented_recent"},
                    "insurance": {"status": "missing"},
                    "maintenance": {"status": "possible_lead"},
                },
            }
        ],
        "uncatalogued": [],
    }


class PublishVendorAuditTests(unittest.TestCase):
    def test_render_report_is_concise_and_links_run(self):
        text = render_report(sample_report(), "https://github.com/example/run")
        self.assertIn("Airtable directory: 1 total / 1 active", text)
        self.assertIn("Insurance / COIs: 0 recent, 0 older, 0 lead(s), 1 missing", text)
        self.assertIn("<https://github.com/example/run|Open the GitHub Actions run>", text)
        self.assertNotIn("Example Vendor", text)

    def test_delivery_resolves_dm_before_posting(self):
        opener = FakeOpener()
        deliver_slack("test report", "token-value", "U123", opener)
        self.assertEqual([r.full_url.rsplit("/", 1)[-1] for r in opener.requests], ["conversations.open", "chat.postMessage"])
        first_payload = json.loads(opener.requests[0].data)
        second_payload = json.loads(opener.requests[1].data)
        self.assertEqual(first_payload, {"users": "U123"})
        self.assertEqual(second_payload["channel"], "DTEST")
        self.assertEqual(second_payload["text"], "test report")
        self.assertEqual(opener.requests[0].headers["Authorization"], "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
