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


class MetadataSessionFake:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return ResponseFake({"tables": self.tables})


class AirtableTests(unittest.TestCase):
    def test_records_uses_canonical_view_and_paginates(self):
        client = AirtableClient("runtime-token", "app25k6lMy8bzOhq5", "tblmysPS8GSncnWSa")
        session = SessionFake()
        client.session = session

        records = client.records(view="viwVD8IFpH6fXUPvh")

        self.assertEqual([record["id"] for record in records], ["rec1", "rec2"])
        self.assertEqual(session.calls[0][1], {"pageSize": 100, "view": "viwVD8IFpH6fXUPvh"})
        self.assertEqual(session.calls[1][1], {"pageSize": 100, "view": "viwVD8IFpH6fXUPvh", "offset": "next-page"})

    def test_schema_resolves_exact_id_and_name_via_metadata_request(self):
        table = {"id": "tbl-vendors", "name": "All Vendors", "fields": [{"id": "fld-name"}]}
        for configured in (table["id"], table["name"]):
            with self.subTest(configured=configured):
                client = AirtableClient("synthetic-token", "app-test", configured)
                session = MetadataSessionFake([{"id": "tbl-other", "name": "Other"}, table])
                client.session = session
                self.assertEqual(client.schema(), table)
                self.assertEqual(session.calls, [
                    ("https://api.airtable.com/v0/meta/bases/app-test/tables", None, 30)
                ])

    def test_schema_prefers_id_over_colliding_name_regardless_of_metadata_order(self):
        target = {"id": "tbl-vendors", "name": "All Vendors", "fields": []}
        collision = {"id": "tbl-other", "name": "tbl-vendors", "fields": []}
        for tables in ([collision, target], [target, collision]):
            with self.subTest(tables=tables):
                client = AirtableClient("synthetic-token", "app-test", "tbl-vendors")
                client.session = MetadataSessionFake(tables)
                self.assertEqual(client.schema(), target)

    def test_schema_missing_table_has_descriptive_non_sensitive_error(self):
        for tables in ([], [{"id": "tbl-other", "name": "Other"}]):
            with self.subTest(tables=tables):
                client = AirtableClient("synthetic-token", "app-test", "missing-private-label")
                client.session = MetadataSessionFake(tables)
                with self.assertRaisesRegex(RuntimeError, "vendor table.*not found.*exact ID or name") as raised:
                    client.schema()
                self.assertNotIn("synthetic-token", str(raised.exception))
                self.assertNotIn("missing-private-label", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
