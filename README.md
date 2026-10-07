# Review Processes

Approval-gated automations for operational reviews. The Vendor Review workflow
compares the live Airtable vendor directory with Gmail contract and service
communications and produces a complete evidence matrix.

## What the audit reviews

For **every record** in the Airtable `All Vendors` table using the
`Every Vendor Ever` view, the audit
reports separate findings for:

1. **Contract / terms** — executed agreements, amendments, renewals, rate
   changes, and terms-of-service documents.
2. **Insurance / COI** — certificates of insurance, policy/declarations pages,
   limits, effective dates, additional-insured language, and workers'
   compensation references.
3. **Maintenance / service** — maintenance agreements, preventive-maintenance
   plans, recurring service, HVAC, refrigeration, plumbing, pest, hood/grease,
   filter, repair, and related service evidence.

The matrix also retains credible Gmail vendors that are not yet in Airtable.
Off-boarded records stay visible but are excluded from the active count.

### Evidence statuses

- `documented_recent` — matching evidence in the configured lookback window;
- `documented_older` — evidence exists in the history window but is older than
  the lookback window;
- `possible_lead` — a solicitation, quote, or inquiry mentions the category but
  does not prove an executed agreement, issued policy, or active service;
- `missing` — no matching Gmail evidence was found in the history window.

A missing insurance document is a finding for human follow-up, not permission
to assume the vendor is uninsured. A solicitation is never treated as an
agreement or coverage proof. Without a recognizable proof attachment, the body must
assert supply of the relevant category's document or contain category-specific details;
a subject title plus an unrelated enclosure remains a lead. Supply uses an explicit
category-document subject/object grammar: an attached invoice *for* an insurance policy
is not supply of that policy. Signed/executed details must describe a completed document
(standalone completed-document heading, possession, or an affirmative document predicate),
not a plan/intention to obtain one. Planned/future recurring maintenance without supplied,
signed, or executed-document evidence also remains a lead; genuinely enclosed agreements
scheduling future service remain evidence.
Conditional and interrogative scope is retained across wrapped plain text and HTML
visual breaks. An independent affirmative enclosure in the body remains evidence even
before a separate request to forward, attach, or upload it; recognizable proof
attachments remain authoritative.

## Safety model

`audit` and `scan` are read-only against Airtable. They create local, atomic
JSON state only. Airtable is changed only by `approve`, which applies the
exact displayed changes after checking for record drift, including optional
answer-derived fields against their scan-time snapshot. Legacy proposals without
those before-values require a rescan unless the field already equals the final
intended value.

Proposal decisions use a private, reentrant `.proposals.lock` covering read through
write (and all approval side effects). This is a POSIX `flock` lock for cooperating
processes sharing a local state directory, not a distributed lock or an Airtable
transaction. Keep the lock file in place; do not delete it while writers run.
Proposal IDs and stable record/kind/evidence identities must be unique; ambiguous
legacy state fails closed and requires manual reconciliation. Audit report storage
is unchanged. Rescans and stale full-list saves preserve every non-pending proposal,
including legacy approved payloads and failed-apply history. Only an explicit replacement
may advance legacy `approved` to `applied`, with all reviewed payload fields unchanged.
Provider-transition replay recognizes both legal and DBA names, so a newly assigned
account on the target provider is not treated as an old supplier account.

The audit stores evidence metadata (message ID, sender, subject, date, Gmail
link, attachment name, and short facts), not the full email body. Gmail is read
with a read-only OAuth scope. Relevant document attachments may be downloaded
for classification; no email is sent and no source attachment is uploaded by
`audit`.

Credentials are runtime inputs only. Never commit `.env`, OAuth files, email
exports, or `.review-state/`.

## Setup

1. Use Python 3.11+ and run:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. Copy `.env.example` to `.env` and set the Airtable values. The default live
   source is the `All Vendors` table / `Every Vendor Ever` view in the `Vendors`
   base. IDs are used so a renamed Airtable label cannot silently redirect the
   audit. `AIRTABLE_TOKEN` is accepted as a backward-compatible alias, but
   `AIRTABLE_API_KEY` is the preferred name.

3. Create a Google Desktop OAuth client with Gmail read-only scope. Save its
   client secret as `google-client-secret.json`, then run this once:

   ```bash
   vendor-review auth
   ```

   For server deployment, provision the resulting token through the runtime
   secret mechanism. Do not commit either OAuth file.

## Use

Run a complete matrix review:

```bash
vendor-review audit --json
vendor-review audit --lookback-days 180 --history-days 730
```

Run the existing approval-proposal scan separately:

```bash
vendor-review scan --json
vendor-review list --status pending
vendor-review show VR-20260826-ABC123
vendor-review answer VR-20260826-ABC123 portal_url https://portal.example.com
vendor-review approve VR-20260826-ABC123 --dry-run
vendor-review approve VR-20260826-ABC123
vendor-review reject VR-20260826-ABC123 --reason "Not a vendor change"
```

`audit` reviews all records and does not create Airtable update proposals for
missing evidence. `scan` remains the narrower workflow for evidence-backed
field changes such as the existing electricity provider transition.

## Configuration

```dotenv
AIRTABLE_API_KEY=...
AIRTABLE_BASE_ID=app25k6lMy8bzOhq5
AIRTABLE_VENDOR_TABLE=tblmysPS8GSncnWSa
AIRTABLE_VENDOR_VIEW=viwVD8IFpH6fXUPvh
GOOGLE_TOKEN_FILE=google-token.json
REVIEW_STATE_DIR=.review-state
REVIEW_LOOKBACK_DAYS=180
REVIEW_HISTORY_DAYS=730
```

The Gmail query uses the history window and includes contract, terms, insurance,
COI, liability, maintenance, HVAC, refrigeration, plumbing, pest, hood, grease,
repair, and service language. The classifier uses document/phrase signals and
sender/domain/name matching, including forwarded-message headers such as
`X-Original-Sender` and `Reply-To`.

## Audit output

The JSON report includes:

- directory and active-directory counts;
- Gmail messages scanned and matched-vendor count;
- one row per Airtable vendor with all three category findings;
- evidence links and attachment names;
- uncatalogued Gmail candidates;
- a missing-finding count for active vendors.

The report is saved at `.review-state/audit-report.json` unless
`REVIEW_STATE_DIR` is changed.

## Completion reporting

GitHub Actions runs `.github/workflows/monthly-vendor-audit.yml` on the first of
each month at 8:00 AM America/Chicago, and on demand from `main`. The job is
read-only against Airtable, uses a Gmail read-only token, and DMs the Slack
recipient stored in `SLACK_RECIPIENT_USER_ID`. Runtime secrets stay in GitHub
encrypted secrets (`AIRTABLE_API_KEY`, `GOOGLE_TOKEN_JSON`, `SLACK_BOT_TOKEN`)
and are shredded at the end of the run.

The existing `reporting.publish_completion` helper can still write Notion before
Slack for a completed local run. The scheduled GitHub job currently delivers the
verified Slack summary only; it does not mutate Airtable or send Gmail.

## Development

Run the complete local test suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover category classification, ordinary-invoice exclusion, insurance
and maintenance solicitation handling, missing evidence, recent-vs-old
history, forwarded sender matching, uncatalogued vendors, off-boarded vendors,
atomic report persistence, the existing electricity detector, and approval
state preservation.
