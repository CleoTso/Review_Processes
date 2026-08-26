from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .airtable import AirtableClient
from .config import Config, load_dotenv
from .gmail import GmailClient, authorize
from .service import VendorReviewService
from .store import ProposalStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vendor-review")
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Create a Gmail read-only OAuth token")
    auth.add_argument("--client-secret", type=Path, default=Path("google-client-secret.json"))
    auth.add_argument("--token-file", type=Path, default=Path("google-token.json"))
    scan = commands.add_parser("scan", help="Read Gmail/Airtable and create proposals")
    scan.add_argument("--lookback-days", type=int)
    scan.add_argument("--required-store")
    scan.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="List proposals")
    listing.add_argument("--status", default="pending")
    listing.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="Show one proposal")
    show.add_argument("proposal_id")
    answer = commands.add_parser("answer", help="Answer a proposal question")
    answer.add_argument("proposal_id")
    answer.add_argument("key")
    answer.add_argument("value")
    approve = commands.add_parser("approve", help="Apply one proposal to Airtable")
    approve.add_argument("proposal_id")
    approve.add_argument("--dry-run", action="store_true")
    reject = commands.add_parser("reject", help="Ignore a proposal without changing Airtable")
    reject.add_argument("proposal_id")
    reject.add_argument("--reason", required=True)
    return root


def dependencies(config: Config):
    store = ProposalStore(config.state_dir)
    airtable = AirtableClient(config.airtable_token, config.airtable_base_id, config.airtable_vendor_table)
    gmail = GmailClient(config.google_token_file)
    return store, VendorReviewService(airtable, gmail, store)


def main() -> None:
    args = parser().parse_args()
    if args.command == "auth":
        authorize(args.client_secret, args.token_file)
        print(f"Saved Gmail read-only token to {args.token_file}")
        return
    if args.command in {"list", "show", "answer", "reject"}:
        load_dotenv()
        store = ProposalStore(Path(os.getenv("REVIEW_STATE_DIR", ".review-state")))
        if args.command == "list":
            proposals = [p for p in store.load() if args.status == "all" or p.status == args.status]
            print(json.dumps([p.to_dict() for p in proposals], indent=2) if args.json else _table(proposals))
        elif args.command == "show":
            print(json.dumps(store.get(args.proposal_id).to_dict(), indent=2))
        elif args.command == "answer":
            proposal = store.get(args.proposal_id)
            question = next((q for q in proposal.questions if q.key == args.key), None)
            if not question:
                raise SystemExit(f"Unknown question key: {args.key}")
            question.answer = args.value
            store.replace(proposal)
            print(f"Saved answer {args.key} for {proposal.id}")
        else:
            proposal = store.get(args.proposal_id)
            proposal.status = "rejected"
            proposal.decision_reason = args.reason
            store.replace(proposal)
            print(f"Rejected and ignored {proposal.id}; Airtable was not changed")
        return
    config = Config.from_env()
    store, service = dependencies(config)
    if args.command == "scan":
        proposals = service.scan(args.lookback_days or config.lookback_days, args.required_store)
        print(json.dumps([p.to_dict() for p in proposals], indent=2) if args.json else _table(proposals))
    elif args.command == "approve":
        proposal = store.get(args.proposal_id)
        if proposal.status == "rejected":
            raise SystemExit("Rejected proposals cannot be applied; run a new scan after correcting the evidence")
        result = service.apply(proposal, args.dry_run)
        print(json.dumps(result, indent=2))


def _table(proposals) -> str:
    if not proposals:
        return "No matching proposals."
    lines = ["ID | Confidence | Store | Vendor | Changes | Questions", "---|---:|---|---|---:|---:"]
    for p in proposals:
        lines.append(
            f"{p.id} | {p.confidence:.0%} | {p.store or '-'} | {p.vendor_before} | "
            f"{len(p.changes)} | {len([q for q in p.questions if not q.answer])}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
