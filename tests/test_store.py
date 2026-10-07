import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_processes.vendor_review.audit import AuditReport
from review_processes.vendor_review.models import AttachmentRef, Evidence, FieldChange, Proposal, Question
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
      self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), [".proposals.lock", "proposals.json"])
      self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o600)
      self.assertEqual(store.load()[0].id, "VR-1")
      # Rewriting an existing file must not loosen or leave residue either.
      store.save([proposal()])
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), [".proposals.lock", "proposals.json"])

  def test_state_is_private_from_creation_without_chmod(self):
    """The state must be private at creation, never written wide then chmod'd down.

    Regression gate: the legacy writer created the temp file with the process
    umask (0644 here) and only chmod'ed 0600 afterwards, leaving a window in
    which the state was world-readable. Forbidding os.chmod entirely makes
    that legacy sequence fail this test deterministically, while a writer
    that is private from creation (mkstemp 0600) succeeds.
    """
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      with mock.patch(
          "os.chmod",
          side_effect=AssertionError(
              "state was written with a wide umask and chmod'ed afterwards"
          ),
      ):
        store.save([proposal()])
      path = Path(directory) / "proposals.json"
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      # No world-readable temp residue with the state content either.
      self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), [".proposals.lock", "proposals.json"])

  def test_failed_replace_leaves_no_residue_and_keeps_previous_state(self):
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      store.save([proposal()])
      path = Path(directory) / "proposals.json"
      before = path.read_text()
      with mock.patch("os.replace", side_effect=OSError("disk full")):
        with self.assertRaises(OSError):
          store.save([proposal()])
      # The previous state is intact and the aborted temp file is cleaned up.
      self.assertEqual(path.read_text(), before)
      self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), [".proposals.lock", "proposals.json"])

  def test_stable_evidence_identity_preserves_decisions_across_dates_and_before_values(self):
    for status in ("applied", "rejected", "approved", "failed"):
      with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
        store = ProposalStore(Path(directory))
        old = proposal()
        old.status = status
        store.upsert([old])
        replay = proposal()
        replay.id = "VR-NEXT-DAY"
        replay.changes[0].before = "After"
        replay.changes.append(FieldChange("Account #", "NEW-ACCOUNT", "", "old evidence"))
        store.upsert([replay])
        self.assertEqual([(p.id, p.status) for p in store.load()], [("VR-1", status)])
        fresh = proposal()
        fresh.id = "VR-FRESH"
        fresh.evidence[0].message_id = "different-evidence"
        store.upsert([fresh])
        self.assertEqual(store.get("VR-FRESH").status, "pending")

  def test_ambiguous_ids_and_legacy_evidence_fail_closed(self):
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      other = proposal()
      other.evidence[0].message_id = "different"
      with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
        store.save([proposal(), other])
      other = proposal()
      other.id = "VR-2"
      with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
        store.save([proposal(), other])
      # Loading already-existing ambiguous JSON also fails before approval.
      import json
      store.path.write_text(json.dumps([proposal().to_dict(), other.to_dict()]))
      with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
        store.get("VR-1")

  def test_full_list_save_preserves_persisted_terminal_decisions(self):
    for status in ("applied", "rejected", "approved", "failed"):
      with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
        store = ProposalStore(Path(directory))
        store.save([proposal()])
        stale = store.load()
        with store.mutate("VR-1") as current:
          current.status = status
        decided = store.get("VR-1").to_dict()
        for snapshot in (stale, []):
          with self.assertRaisesRegex(RuntimeError, "Decided"):
            store.save(snapshot)
          self.assertEqual(store.get("VR-1").to_dict(), decided)
        changed = store.load()
        changed[0].changes[0].after = "different"
        with self.assertRaisesRegex(RuntimeError, "Decided"):
          store.save(changed)
        self.assertEqual(store.get("VR-1").to_dict(), decided)
        # An identical terminal snapshot is still a legitimate save.
        store.save(store.load())

  def test_legacy_approved_and_failed_payloads_survive_rescans_and_replace(self):
    for status in ("approved", "failed"):
      with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
        store = ProposalStore(Path(directory))
        old = proposal()
        old.status = status
        old.questions = [Question("billing_account", "Reviewed account?", answer="REVIEWED")]
        old.source_fields = {"Account #": "OLD"}
        old.attachments = [AttachmentRef("m1", "agreement.pdf")]
        old.error = "Previous failed apply attempt" if status == "failed" else None
        old.decision_reason = "Reviewed original evidence"
        store.save([old])
        expected = old.to_dict()
        replay = Proposal.from_dict(old.to_dict())
        replay.status = "pending"
        replay.id = "VR-NEXT-DAY"
        replay.changes[0].after = "UNREVIEWED"
        replay.questions[0].answer = "UNREVIEWED"
        replay.source_fields["Account #"] = "NEW"
        replay.error = None
        store.upsert([replay])
        self.assertEqual(store.get(old.id).to_dict(), expected)
        changed = store.get(old.id)
        changed.changes[0].after = "UNREVIEWED"
        with self.assertRaisesRegex(RuntimeError, "Decided"):
          store.replace(changed)
        self.assertEqual(store.get(old.id).to_dict(), expected)
        if status == "failed":
          changed = store.get(old.id)
          changed.status = "applied"
          with self.assertRaisesRegex(RuntimeError, "Decided"):
            store.replace(changed)
          self.assertEqual(store.get(old.id).to_dict(), expected)

  def test_only_explicit_unchanged_legacy_approval_can_be_applied(self):
    with tempfile.TemporaryDirectory() as directory:
      store = ProposalStore(Path(directory))
      old = proposal()
      old.status = "approved"
      old.questions = [Question("billing_account", "Account?", answer="REVIEWED")]
      old.source_fields = {"Account #": "OLD"}
      store.save([old])
      applied = store.get(old.id)
      applied.status = "applied"
      # A stale full-list save is not an explicit application transition.
      with self.assertRaisesRegex(RuntimeError, "Decided"):
        store.save([applied])
      for field in ("changes", "questions", "source_fields", "evidence", "decision_reason"):
        changed = Proposal.from_dict(applied.to_dict())
        if field == "changes":
          changed.changes[0].after = "UNREVIEWED"
        elif field == "questions":
          changed.questions[0].answer = "UNREVIEWED"
        elif field == "source_fields":
          changed.source_fields["Account #"] = "NEW"
        elif field == "evidence":
          changed.evidence[0].subject = "Different evidence"
        else:
          changed.decision_reason = "Different decision"
        with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "Decided"):
          store.replace(changed)
        self.assertEqual(store.get(old.id).to_dict(), old.to_dict())
      store.replace(applied)
      self.assertEqual(store.get(old.id).to_dict(), applied.to_dict())
      store.save(store.load())

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
