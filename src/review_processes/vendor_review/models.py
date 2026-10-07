from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Literal


Status = Literal["pending", "approved", "rejected", "applied", "failed"]


@dataclass(slots=True)
class Evidence:
    message_id: str
    subject: str
    sender: str
    received_at: str
    gmail_url: str
    attachment_name: str | None = None
    facts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FieldChange:
    field_name: str
    before: Any
    after: Any
    reason: str


@dataclass(slots=True)
class AttachmentRef:
    message_id: str
    filename: str
    airtable_field: str = "Contracts & Warranties"


@dataclass(slots=True)
class Question:
    key: str
    prompt: str
    required: bool = False
    answer: str | None = None


@dataclass(slots=True)
class Proposal:
    id: str
    kind: str
    record_id: str
    vendor_before: str
    store: str | None
    confidence: float
    changes: list[FieldChange]
    evidence: list[Evidence]
    attachments: list[AttachmentRef] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    status: Status = "pending"
    decision_reason: str | None = None
    error: str | None = None
    # Scan-time values for fields that optional answers can write. None means
    # legacy JSON, not a snapshot of empty fields.
    source_fields: dict[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        # Evidence identity survives application, mutable field values and dates.
        payload = json.dumps([
            self.kind, self.record_id,
            sorted({(e.message_id, e.attachment_name or "") for e in self.evidence}),
            sorted({(a.message_id, a.filename) for a in self.attachments}),
        ])
        return sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Proposal":
        return cls(
            **{
                **value,
                "changes": [FieldChange(**x) for x in value.get("changes", [])],
                "evidence": [Evidence(**x) for x in value.get("evidence", [])],
                "attachments": [AttachmentRef(**x) for x in value.get("attachments", [])],
                "questions": [Question(**x) for x in value.get("questions", [])],
            }
        )

