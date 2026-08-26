from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .models import Proposal


NOTION_AUDIT_PAGE = "https://app.notion.com/p/3c871e784c17805e8a8ffe93cb4af806"


@dataclass(frozen=True, slots=True)
class AuditCompletion:
    audit_date: date
    proposals: list[Proposal] = field(default_factory=list)
    sources_reviewed: list[str] = field(default_factory=lambda: ["Airtable Vendors", "Gmail vendor documents"])
    notes: list[str] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        return sum(proposal.status == "pending" for proposal in self.proposals)


def render_notion_entry(completion: AuditCompletion) -> str:
    """Render a self-contained entry suitable for insertion at the page start."""
    lines = [
        f"## Vendor Audit — {completion.audit_date:%B %-d, %Y}",
        f"**Status:** Completed — {completion.pending_count} update proposal(s) awaiting approval",
        "**Sources reviewed:** " + ", ".join(completion.sources_reviewed),
    ]
    if completion.proposals:
        lines.append("### Proposed updates")
        for proposal in completion.proposals:
            change_names = ", ".join(change.field_name for change in proposal.changes)
            lines.append(
                f"- **{proposal.id}:** {proposal.vendor_before}"
                f" ({proposal.store or 'all stores'}; {proposal.confidence:.0%} confidence) — {change_names}"
            )
    else:
        lines.append("No evidence-backed vendor updates were identified.")
    if completion.notes:
        lines.append("### Notes")
        lines.extend(f"- {note}" for note in completion.notes)
    lines.extend([
        "Airtable remained unchanged during the audit. Approved proposals are applied separately.",
        "---",
    ])
    return "\n".join(lines)


def render_slack_dm(completion: AuditCompletion, notion_url: str = NOTION_AUDIT_PAGE) -> str:
    return (
        f"*Vendor Audit completed — {completion.audit_date:%B %-d, %Y}*\n"
        f"{completion.pending_count} update proposal(s) are awaiting approval. "
        f"Dated notes were added to <{notion_url}|Vendor Audit Automation>."
    )


def publish_completion(
    completion: AuditCompletion,
    prepend_notion: Callable[[str], None],
    send_slack_dm: Callable[[str], None],
) -> None:
    """Publish in the required order; Slack is never sent if Notion fails."""
    prepend_notion(render_notion_entry(completion))
    send_slack_dm(render_slack_dm(completion))

