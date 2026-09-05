import stat
import tempfile
import unittest
from pathlib import Path

from review_processes.vendor_review.audit import AuditReport
from review_processes.vendor_review.models import Evidence, FieldChange, Proposal
from review_processes.vendor_review.store import AuditReportStore, ProposalStore


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

  def test_proposal_state_is_private_and_leaves_no_temp_files(self):
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      store.save([proposal()])
      path = Path(directory) / "proposals.json"
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual([item.name for item in Path(directory).iterdir()], ["proposals.json"])
      self.assertEqual(store.load()[0].id, "VR-1")
      # Rewriting an existing file must not loosen or leave residue either.
      store.save([proposal()])
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual([item.name for item in Path(directory).iterdir()], ["proposals.json"])

  def test_audit_report_state_is_private_and_leaves_no_temp_files(self):
    with tempfile.TemporaryDirectory() as directory:
      store = AuditReportStore(Path(directory))
      store.save(AuditReport(
          generated_at="2026-09-05T00:00:00+00:00", lookback_days=90, history_days=730,
          directory_count=0, active_directory_count=0, messages_scanned=0,
          matched_vendor_count=0, vendors=[],
      ))
      path = Path(directory) / "audit-report.json"
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual([item.name for item in Path(directory).iterdir()], ["audit-report.json"])
      self.assertEqual(store.load().directory_count, 0)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
