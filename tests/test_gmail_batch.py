import json
import unittest

from review_processes.vendor_review.gmail import _parse_batch_messages


class GmailBatchParseTests(unittest.TestCase):
    def test_extracts_message_payloads_from_batch_body(self):
        first = {"id": "aaa", "payload": {"headers": [{"name": "Subject", "value": "COI"}]}}
        second = {"id": "bbb", "payload": {"headers": [{"name": "From", "value": "a@b.com"}]}}
        body = (
            "--batch\r\nHTTP/1.1 200 OK\r\n\r\n"
            + json.dumps(first)
            + "\r\n--batch\r\nHTTP/1.1 200 OK\r\n\r\n"
            + json.dumps(second)
            + "\r\n--batch--"
        )
        parsed = _parse_batch_messages(body)
        self.assertEqual(set(parsed), {"aaa", "bbb"})
        self.assertEqual(parsed["aaa"]["payload"]["headers"][0]["value"], "COI")


if __name__ == "__main__":
    unittest.main()
