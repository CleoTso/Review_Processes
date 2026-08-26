import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_processes.vendor_review.config import Config


class ConfigTests(unittest.TestCase):
    def test_prefers_runtime_api_key_and_canonical_vendor_source_default(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "AIRTABLE_API_KEY": "runtime-value",
                    "AIRTABLE_BASE_ID": "base-value",
                    "AIRTABLE_VENDOR_TABLE": "",
                    "GOOGLE_TOKEN_FILE": "token.json",
                    "REVIEW_STATE_DIR": directory,
                },
                clear=True,
            ):
                config = Config.from_env()
        self.assertEqual(config.airtable_token, "runtime-value")
        self.assertEqual(config.airtable_base_id, "base-value")
        self.assertEqual(config.airtable_vendor_table, "tblmysPS8GSncnWSa")
        self.assertEqual(config.airtable_vendor_view, "viwVD8IFpH6fXUPvh")
        self.assertEqual(config.history_days, 730)

    def test_legacy_airtable_token_remains_accepted(self):
        with patch.dict(
            os.environ,
            {"AIRTABLE_TOKEN": "legacy-value", "AIRTABLE_BASE_ID": "base-value"},
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.airtable_token, "legacy-value")


if __name__ == "__main__":
    unittest.main()
