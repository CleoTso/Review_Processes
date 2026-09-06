"""Offline fault-injection tests for GmailClient batch and fallback durability.

These tests gate deterministic failure points (HTTP status injection) so that
transient transport failures cannot silently drop messages from the vendor
audit evidence matrix.
"""

import json
import tempfile
import unittest
from pathlib import Path

import requests

from review_processes.vendor_review.gmail import GmailClient


def message_payload(message_id):
    return {
        "id": message_id,
        "internalDate": "1725148800000",
        "payload": {"headers": [{"name": "Subject", "value": "COI"}]},
    }


class ResponseFake:
    def __init__(self, status_code=200, text="", body=None):
        self.status_code = status_code
        self.text = text
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def json(self):
        return self._body


class SessionFake:
    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response or ResponseFake()
        self.post_response = post_response or ResponseFake()
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append(url)
        return self.get_response

    def post(self, url, data=None, headers=None, timeout=None):
        self.post_calls.append(url)
        return self.post_response


def client_with(session):
    with tempfile.TemporaryDirectory() as directory:
        token = Path(directory) / "google-token.json"
        token.write_text(json.dumps({
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "expiry": "2099-01-01T00:00:00Z",
        }))
        client = GmailClient(token)
    client.session = session
    return client


class GmailBatchDurabilityTests(unittest.TestCase):
    def test_batch_rate_limit_fails_loudly_instead_of_dropping_messages(self):
        # A 429 on the batch endpoint previously returned {} and the fallback
        # swallowed every per-message error, so the whole chunk vanished from
        # the audit without any signal.
        session = SessionFake(post_response=ResponseFake(status_code=429))
        client = client_with(session)

        with self.assertRaises(RuntimeError) as caught:
            client.messages(["m1", "m2"], format="full")

        self.assertIn("429", str(caught.exception))
        self.assertIn("2 message(s)", str(caught.exception))

    def test_batch_server_error_fails_loudly(self):
        session = SessionFake(post_response=ResponseFake(status_code=503))
        client = client_with(session)

        with self.assertRaises(RuntimeError):
            client.messages(["m1"], format="full")

    def test_deleted_message_is_skipped_while_readable_ones_are_kept(self):
        # Batch succeeded but one id was deleted between search and fetch:
        # the individual fetch reports 404 and only that message is skipped.
        session = SessionFake(
            post_response=ResponseFake(
                body={},
                text=json.dumps(message_payload("m1")),
            ),
            get_response=ResponseFake(status_code=404),
        )
        client = client_with(session)

        loaded = client.messages(["m1", "gone"], format="full")

        self.assertEqual([item["id"] for item in loaded], ["m1"])
        self.assertEqual(len(session.get_calls), 1)  # only the missing id

    def test_transient_single_message_failure_propagates(self):
        # A 429 on the individual fallback must not silently skip the message.
        session = SessionFake(
            post_response=ResponseFake(body={}, text=""),
            get_response=ResponseFake(status_code=429),
        )
        client = client_with(session)

        with self.assertRaises(requests.HTTPError):
            client.messages(["m1"], format="full")


if __name__ == "__main__":
    unittest.main()
