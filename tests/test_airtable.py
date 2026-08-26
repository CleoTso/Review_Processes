import unittest

from review_processes.vendor_review.airtable import AirtableClient


class ResponseFake:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class SessionFake:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if len(self.calls) == 1:
            return ResponseFake({"records": [{"id": "rec1"}], "offset": "next-page"})
        return ResponseFake({"records": [{"id": "rec2"}]})


class AirtableTests(unittest.TestCase):
    def test_records_uses_canonical_view_and_paginates(self):
        client = AirtableClient("runtime-token", "app25k6lMy8bzOhq5", "tblmysPS8GSncnWSa")
        session = SessionFake()
        client.session = session

        records = client.records(view="viwVD8IFpH6fXUPvh")

        self.assertEqual([record["id"] for record in records], ["rec1", "rec2"])
        self.assertEqual(session.calls[0][1], {"pageSize": 100, "view": "viwVD8IFpH6fXUPvh"})
        self.assertEqual(session.calls[1][1], {"pageSize": 100, "view": "viwVD8IFpH6fXUPvh", "offset": "next-page"})


if __name__ == "__main__":
    unittest.main()
