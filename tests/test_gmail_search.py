import unittest
from unittest.mock import Mock

from review_processes.vendor_review.gmail import GmailClient
from review_processes.vendor_review.service import VendorReviewService


class ResponseFake:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class SessionFake:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return ResponseFake(self.pages[len(self.calls) - 1])


def page(count, start=0, token=None):
    body = {"messages": [{"id": f"m{index}"} for index in range(start, start + count)]}
    if token is not None:
        body["nextPageToken"] = token
    return body


def client_with_pages(pages):
    # Bypass OAuth entirely: no token file, credentials, or real session.
    client = GmailClient.__new__(GmailClient)
    client.session = SessionFake(pages)
    return client


class GmailSearchTests(unittest.TestCase):
    def test_empty_results(self):
        for body in ({}, {"messages": []}):
            with self.subTest(body=body):
                client = client_with_pages([body])
                self.assertEqual(client.search("synthetic-query"), [])
                self.assertEqual(len(client.session.calls), 1)

    def test_complete_pagination_bounds_requests_to_remaining_budget_and_500(self):
        client = client_with_pages([
            page(500, token="synthetic-page-2"),
            page(99, start=500, token="synthetic-page-3"),
            page(1, start=599),
        ])

        self.assertEqual(client.search("synthetic-query", max_results=650),
                         [f"m{index}" for index in range(600)])
        self.assertEqual(client.session.calls, [
            (f"{GmailClient.API}/messages", {"q": "synthetic-query", "maxResults": 500}, 30),
            (f"{GmailClient.API}/messages", {
                "q": "synthetic-query", "maxResults": 150, "pageToken": "synthetic-page-2",
            }, 30),
            (f"{GmailClient.API}/messages", {
                "q": "synthetic-query", "maxResults": 51, "pageToken": "synthetic-page-3",
            }, 30),
        ])

    def test_exact_cap_without_continuation_is_complete(self):
        for limit in (1, 500):
            with self.subTest(limit=limit):
                client = client_with_pages([page(limit)])
                self.assertEqual(client.search("synthetic-query", max_results=limit),
                                 [f"m{index}" for index in range(limit)])
                self.assertEqual(len(client.session.calls), 1)

        client = client_with_pages([
            page(2, token="synthetic-page-2"), page(3, start=2),
        ])
        self.assertEqual(client.search("synthetic-query", max_results=5),
                         [f"m{index}" for index in range(5)])
        self.assertEqual([call[1]["maxResults"] for call in client.session.calls], [5, 3])

    def test_exact_cap_with_continuation_fails_without_an_extra_request(self):
        for pages in (
            [page(5, token="synthetic-continuation")],
            [page(2, token="synthetic-page-2"),
             page(3, start=2, token="synthetic-continuation")],
        ):
            with self.subTest(page_count=len(pages)):
                client = client_with_pages(pages)
                with self.assertRaisesRegex(RuntimeError, "max_results.*incomplete") as caught:
                    client.search("synthetic-private-query", max_results=5)
                self.assertEqual(len(client.session.calls), len(pages))
                for sensitive in ("synthetic-private-query", "synthetic-continuation", "m0"):
                    self.assertNotIn(sensitive, str(caught.exception))

    def test_overfull_response_fails_even_without_continuation(self):
        for limit, pages in (
            (2, [page(3)]),
            (600, [page(501)]),  # Below the total cap, but above the per-request limit.
            (5, [page(3, token="synthetic-page-2"), page(3, start=3)]),
        ):
            with self.subTest(limit=limit, page_count=len(pages)):
                client = client_with_pages(pages)
                with self.assertRaisesRegex(RuntimeError, "more messages than requested.*incomplete"):
                    client.search("synthetic-query", max_results=limit)
                self.assertEqual(len(client.session.calls), len(pages))

    def test_empty_page_with_continuation_still_paginates(self):
        client = client_with_pages([
            {"nextPageToken": "synthetic-page-2"}, page(1),
        ])
        self.assertEqual(client.search("synthetic-query", max_results=2), ["m0"])
        self.assertEqual([call[1]["maxResults"] for call in client.session.calls], [2, 2])

    def test_invalid_limits_fail_before_requesting(self):
        for limit in (0, -1, True, False, None, "5", 1.5, float("inf"), float("nan")):
            with self.subTest(limit=limit):
                client = client_with_pages([])
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    client.search("synthetic-query", max_results=limit)
                self.assertEqual(client.session.calls, [])

    def test_audit_does_not_fetch_evidence_or_save_report_when_search_is_incomplete(self):
        client = client_with_pages([
            page(500, start=index * 500, token=f"synthetic-page-{index + 2}")
            for index in range(4)
        ])
        client.messages = Mock()
        airtable = Mock()
        airtable.records.return_value = []
        audit_store = Mock()
        service = VendorReviewService(airtable, client, Mock(), audit_store)

        with self.assertRaisesRegex(RuntimeError, "max_results.*incomplete"):
            service.audit(lookback_days=90)

        self.assertEqual(len(client.session.calls), 4)
        client.messages.assert_not_called()
        audit_store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
