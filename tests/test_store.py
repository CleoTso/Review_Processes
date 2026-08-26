import tempfile
import unittest
from pathlib import Path

from review_processes.vendor_review.models import Evidence, FieldChange, Proposal
from review_processes.vendor_review.store import ProposalStore


def proposal():
    return Proposal(
        id="VR-1", kind="test", record_id="rec1", vendor_before="Before", store=None,
        confidence=1.0, changes=[FieldChange("Vendor", "Before", "After", "proof")],
        evidence=[Evidence("m1", "subject", "sender", "date", "url")],
    )


class StoreTests(unittest.TestCase):
  def test_rejection_survives_rescan(self):
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      item = proposal()
      store.upsert([item])
      item.status = "rejected"
      store.replace(item)
      store.upsert([proposal()])
      self.assertEqual(store.get("VR-1").status, "rejected")


if __name__ == "__main__":
    unittest.main()
