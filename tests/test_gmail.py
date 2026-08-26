import stat
import tempfile
import unittest
from pathlib import Path

from review_processes.vendor_review.gmail import _write_private_token


class GmailTokenTests(unittest.TestCase):
    def test_token_is_private_and_atomic_write_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "google-token.json"
            _write_private_token(path, "test-token-material")
            self.assertEqual(path.read_text(), "test-token-material")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(".*")), [])


if __name__ == "__main__":
    unittest.main()
