from __future__ import annotations

import json
import os
from pathlib import Path

from .audit import AuditReport
from .models import Proposal


class ProposalStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "proposals.json"

    def load(self) -> list[Proposal]:
        if not self.path.exists():
            return []
        return [Proposal.from_dict(x) for x in json.loads(self.path.read_text())]

    def save(self, proposals: list[Proposal]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps([p.to_dict() for p in proposals], indent=2, default=str))
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def upsert(self, incoming: list[Proposal]) -> list[Proposal]:
        existing = self.load()
        by_fingerprint = {p.fingerprint: p for p in existing}
        for proposal in incoming:
            old = by_fingerprint.get(proposal.fingerprint)
            if old and old.status in {"rejected", "applied"}:
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

    def replace(self, updated: Proposal) -> None:
        proposals = self.load()
        for index, proposal in enumerate(proposals):
            if proposal.id == updated.id:
                proposals[index] = updated
                self.save(proposals)
                return
        raise SystemExit(f"Unknown proposal: {updated.id}")


class AuditReportStore:
    """Atomic local persistence for complete read-only audit reports."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "audit-report.json"

    def save(self, report: AuditReport) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(report.to_dict(), indent=2))
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def load(self) -> AuditReport:
        if not self.path.exists():
            raise SystemExit(f"No audit report at {self.path}")
        return AuditReport.from_dict(json.loads(self.path.read_text()))
