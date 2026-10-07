from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

# POSIX advisory locks: all writers must share this local state directory.
import fcntl

from .audit import AuditReport
from .fsutil import write_private_atomic
from .models import Proposal


class ProposalStore:
    _locks: dict[str, tuple[threading.RLock, threading.local]] = {}
    _registry_lock = threading.Lock()

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "proposals.json"
        self.lock_path = state_dir / ".proposals.lock"
        with self._registry_lock:
            self._lock, self._local = self._locks.setdefault(
                str(self.path.resolve()), (threading.RLock(), threading.local())
            )

    @contextmanager
    def transaction(self):
        """Serialize a whole decision, including external approval side effects.

        Reentrant across store instances in one thread; process-safe on POSIX
        local filesystems. Never unlink the lock file (its inode is the lock).
        """
        with self._lock:
            if getattr(self._local, "depth", 0):
                self._local.depth += 1
                try:
                    yield self
                finally:
                    self._local.depth -= 1
                return
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._local.depth = 1
                try:
                    yield self
                finally:
                    self._local.depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _validate(proposals: list[Proposal]) -> None:
        ids, identities = set(), set()
        for proposal in proposals:
            if proposal.id in ids or proposal.fingerprint in identities:
                raise RuntimeError("Ambiguous legacy proposal IDs/evidence; reconcile state before rescan or approval")
            ids.add(proposal.id)
            identities.add(proposal.fingerprint)

    def load(self) -> list[Proposal]:
        with self.transaction():
            if not self.path.exists():
                return []
            proposals = [Proposal.from_dict(x) for x in json.loads(self.path.read_text())]
            self._validate(proposals)
            return proposals

    @staticmethod
    def _unchanged_approval(old: Proposal, new: Proposal) -> bool:
        """Only application of the exact legacy approved payload is permitted."""
        if old.status != "approved" or new.status != "applied":
            return False
        expected = old.to_dict()
        expected["status"] = "applied"
        return expected == new.to_dict()

    def save(self, proposals: list[Proposal]) -> None:
        with self.transaction():
            self._save(proposals)

    def _save(self, proposals: list[Proposal], *, applied_id: str | None = None) -> None:
        # Caller holds the transaction. Full-list saves cannot make decisions;
        # replace alone authorizes an unchanged approved -> applied transition.
        self._validate(proposals)
        by_id = {proposal.id: proposal for proposal in proposals}
        for persisted in self.load():
            if persisted.status != "pending":
                supplied = by_id.get(persisted.id)
                if supplied is None or supplied.to_dict() != persisted.to_dict():
                    if not (supplied is not None and persisted.id == applied_id
                            and self._unchanged_approval(persisted, supplied)):
                        raise RuntimeError("Decided proposals cannot be removed or changed by save")
        write_private_atomic(
            self.path, json.dumps([p.to_dict() for p in proposals], indent=2, default=str)
        )

    def upsert(self, incoming: list[Proposal]) -> list[Proposal]:
        with self.transaction():
            return self._upsert(incoming)

    def _upsert(self, incoming: list[Proposal]) -> list[Proposal]:
        existing = self.load()
        by_fingerprint = {p.fingerprint: p for p in existing}
        for proposal in incoming:
            old = by_fingerprint.get(proposal.fingerprint)
            if old and old.status != "pending":
                continue
            if old:
                proposal.id = old.id
                proposal.status = old.status
                proposal.questions = self._preserve_answers(proposal, old)
            by_fingerprint[proposal.fingerprint] = proposal
        result = sorted(by_fingerprint.values(), key=lambda p: p.id)
        self.save(result)
        return result

    @staticmethod
    def _preserve_answers(new: Proposal, old: Proposal):
        answers = {q.key: q.answer for q in old.questions}
        for question in new.questions:
            question.answer = answers.get(question.key)
        return new.questions

    def get(self, proposal_id: str) -> Proposal:
        for proposal in self.load():
            if proposal.id == proposal_id:
                return proposal
        raise SystemExit(f"Unknown proposal: {proposal_id}")

    @contextmanager
    def mutate(self, proposal_id: str):
        """Read current state and commit the caller's mutation under one lock."""
        with self.transaction():
            proposal = self.get(proposal_id)
            yield proposal
            self.replace(proposal)

    def replace(self, updated: Proposal) -> None:
        with self.transaction():
            proposals = self.load()
            for index, proposal in enumerate(proposals):
                if proposal.id == updated.id:
                    if (proposal.status != "pending" and proposal.to_dict() != updated.to_dict()
                            and not self._unchanged_approval(proposal, updated)):
                        raise RuntimeError("Decided proposals cannot be replaced; rescan before changing a decision")
                    proposals[index] = updated
                    self._save(proposals, applied_id=updated.id)
                    return
            raise SystemExit(f"Unknown proposal: {updated.id}")


class AuditReportStore:
    """Atomic local persistence for complete read-only audit reports."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "audit-report.json"

    def save(self, report: AuditReport) -> None:
        write_private_atomic(self.path, json.dumps(report.to_dict(), indent=2))

    def load(self) -> AuditReport:
        if not self.path.exists():
            raise SystemExit(f"No audit report at {self.path}")
        return AuditReport.from_dict(json.loads(self.path.read_text()))
