"""Hermetic subprocess interleavings. Pipes, not sleeps, choose serialization."""
import fcntl
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from review_processes.vendor_review.models import Evidence, FieldChange, Proposal, Question
from review_processes.vendor_review.service import VendorReviewService
from review_processes.vendor_review.store import ProposalStore


def item():
    return Proposal(
        id="VR-1", kind="test", record_id="rec", vendor_before="Old", store=None,
        confidence=1, changes=[FieldChange("Vendor", "Old", "New", "proof")],
        evidence=[Evidence("m", "", "", "", "")],
        questions=[Question("portal_url", "portal")], source_fields={"Website": None},
    )


def witness_contention(store, pipe):
    # Deterministically prove another process holds the SAME inode. If a scan
    # only locked its final save, or approval released before update, this fails.
    fd = os.open(store.lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pipe.send("contended")
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            pipe.send("NOT LOCKED")
    finally:
        os.close(fd)


class HeldScanStore(ProposalStore):
    def __init__(self, directory, pipe):
        super().__init__(Path(directory))
        self.pipe = pipe
        self.held = False

    def load(self):
        proposals = super().load()
        if not self.held:
            self.held = True
            self.pipe.send("read")
            assert self.pipe.recv() == "release"
        return proposals


class ApplyFake:
    def __init__(self, directory, pipe=None):
        self.directory, self.pipe = Path(directory), pipe

    def record(self, record_id):
        return {"fields": {"Vendor": "Old"}}

    def update(self, record_id, fields):
        if self.pipe:
            self.pipe.send("update")
            assert self.pipe.recv() == "release"
        with (self.directory / "writes").open("a") as handle:
            handle.write("update\n")


def worker(directory, action, pipe, held=False, contend=False):
    store = HeldScanStore(directory, pipe) if action == "scan" and held else ProposalStore(Path(directory))
    # Read the displayed proposal before competing approval enters its lock.
    stale = item() if action == "approve" else None
    if contend:
        witness_contention(store, pipe)
    try:
        if action == "scan":
            store.upsert([item()])
        elif action == "approve":
            VendorReviewService(ApplyFake(directory, pipe if held else None), object(), store).apply(stale)
        else:
            with store.mutate("VR-1") as proposal:
                if proposal.status != "pending":
                    raise RuntimeError("decided")
                if held:
                    pipe.send("read")
                    assert pipe.recv() == "release"
                if action == "answer":
                    proposal.questions[0].answer = "https://portal.example"
                else:
                    proposal.status = "rejected"
                    proposal.decision_reason = "human decision"
        pipe.send("done")
    except RuntimeError as error:
        pipe.send("refused:" + str(error))
    finally:
        pipe.close()


@unittest.skipUnless(os.name == "posix", "POSIX local flock scope")
class ProcessTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProposalStore(Path(self.directory.name))
        self.store.upsert([item()])
        self.context = multiprocessing.get_context("spawn")

    def start(self, action, **kwargs):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=worker, args=(self.directory.name, action, child), kwargs=kwargs)
        process.start()
        child.close()
        self.addCleanup(parent.close)
        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.addCleanup(cleanup)
        return parent, process

    def receive(self, pipe):
        self.assertTrue(pipe.poll(10), "subprocess synchronization timed out")
        return pipe.recv()

    def finish(self, process):
        process.join(10)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)

    def test_scan_cannot_undo_rejection(self):
        scan, scan_process = self.start("scan", held=True)
        self.assertEqual(self.receive(scan), "read")
        reject, reject_process = self.start("reject", contend=True)
        self.assertEqual(self.receive(reject), "contended")
        scan.send("release")
        self.assertEqual(self.receive(scan), "done")
        self.assertEqual(self.receive(reject), "done")
        self.finish(scan_process)
        self.finish(reject_process)
        self.assertEqual(self.store.get("VR-1").status, "rejected")
        # A later stale scan also preserves the now-durable decision.
        self.store.upsert([item()])
        self.assertEqual(self.store.get("VR-1").status, "rejected")

    def test_answer_then_reject_preserves_answer_and_decision(self):
        self.decisions("answer", "reject", "done", "https://portal.example")

    def test_reject_then_answer_refuses_answer_without_undoing_decision(self):
        self.decisions("reject", "answer", "refused:decided", None)

    def decisions(self, first_action, second_action, second_result, answer):
        first, first_process = self.start(first_action, held=True)
        self.assertEqual(self.receive(first), "read")
        second, second_process = self.start(second_action, contend=True)
        self.assertEqual(self.receive(second), "contended")
        first.send("release")
        self.assertEqual(self.receive(first), "done")
        self.assertEqual(self.receive(second), second_result)
        self.finish(first_process)
        self.finish(second_process)
        proposal = self.store.get("VR-1")
        self.assertEqual(proposal.status, "rejected")
        self.assertEqual(proposal.questions[0].answer, answer)

    def test_competing_approvals_do_not_both_write(self):
        first, first_process = self.start("approve", held=True)
        self.assertEqual(self.receive(first), "update")
        second, second_process = self.start("approve", contend=True)
        self.assertEqual(self.receive(second), "contended")
        first.send("release")
        self.assertEqual(self.receive(first), "done")
        self.assertIn("refused:Proposal is applied", self.receive(second))
        self.finish(first_process)
        self.finish(second_process)
        self.assertEqual((Path(self.directory.name) / "writes").read_text(), "update\n")
        self.assertEqual(self.store.get("VR-1").status, "applied")

    def test_nested_instances_share_reentrant_transaction(self):
        other = ProposalStore(Path(self.directory.name))
        with self.store.transaction(), other.transaction(), other.mutate("VR-1") as proposal:
            proposal.questions[0].answer = "nested"
        self.assertEqual(self.store.get("VR-1").questions[0].answer, "nested")
