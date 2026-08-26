# Review Processes

Approval-gated automations for operational reviews. The first workflow compares the Airtable vendor directory with recent Gmail invoices, bills, contracts, and agreements.

## Safety model

`scan` is read-only. It creates a local proposal queue with evidence, confidence, the exact before/after values, attachments to save, and any questions. Airtable is only changed by `approve`. `reject` records a local rejection so the proposal is ignored on future scans. Before applying an approval, the tool checks that the Airtable values have not changed since the scan.

No Gmail messages, contracts, credentials, or proposal state are committed; `.review-state/` and OAuth files are ignored.

## Setup

1. Use Python 3.11+ and run `python -m venv .venv && source .venv/bin/activate`.
2. Install with `pip install -e .`.
3. Copy `.env.example` to `.env` and set the Airtable base/table values.
4. Create a Google Desktop OAuth client with Gmail read-only scope. Save its secret as `google-client-secret.json`, then run `vendor-review auth` once. For server deployment, provision `google-token.json` through your secrets manager.

The Airtable token needs record read/write access and attachment-upload access for the configured base. Never commit either credential file.

## Use

```bash
vendor-review scan
vendor-review list
vendor-review show VR-20260826-ABC123
vendor-review approve VR-20260826-ABC123
vendor-review reject VR-20260826-ABC123 --reason "Not a vendor change"
```

`approve` applies only the displayed fields and uploads only the displayed contract/agreement attachments. If a proposal has unresolved required questions, pass their answers first:

```bash
vendor-review answer VR-20260826-ABC123 portal_url https://portal.example.com
vendor-review approve VR-20260826-ABC123
```

Useful options:

```bash
vendor-review scan --lookback-days 90 --required-store RoundRock
vendor-review list --status pending
vendor-review approve ID --dry-run
```

## Automation

Run `vendor-review scan --json` on a schedule and send the resulting pending proposals to the desired notification channel. Keep the approval command behind an authenticated human action. The CLI exits non-zero if a required store has no evidence candidate, preventing a silent miss.

Review automations default to a monthly recurrence on the calendar day they are created unless a review specifies another cadence. The Vendor Review was created on August 26, 2026 and runs on the 26th of each month. Scheduled scans remain read-only; approvals are separate human actions.

### Completion reporting

After every completed Vendor Audit, the automation must complete these actions in order:

1. Prepend a dated audit entry to the [Vendor Audit Automation Notion page](https://app.notion.com/p/3c871e784c17805e8a8ffe93cb4af806). The newest audit must remain at the top.
2. After the Notion update succeeds, send Cleo a Slack DM confirming completion and linking to that page. A self-DM is acceptable.

The dated note includes sources reviewed, proposal IDs and affected vendors/stores, confidence, fields proposed for change, unanswered questions, and confirmation that the read-only audit did not modify Airtable. `reporting.publish_completion` enforces Notion-before-Slack ordering and does not send a success DM when the Notion write fails.

## Scope and evidence rules

- Invoices establish that a vendor/account is active, but do not by themselves prove a legal-name or portal change.
- Executed agreements can propose provider, term, price, notice contact, service address, and contract attachment updates.
- Sender addresses are evidence only when the sender domain belongs to the vendor or the document labels the address as a notice/support contact.
- A supplier change preserves existing attachments and appends the new agreement.
- Unknown portal URLs, account numbers, or switch dates become questions; they are never guessed.
