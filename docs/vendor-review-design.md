# Complete Vendor Review Design

## Goal

Produce a read-only review matrix for every vendor service in the Airtable
vendor directory, plus credible vendor candidates found in Gmail. For each
vendor, inspect recent and historical Gmail evidence for:

- contracts and commercial terms;
- insurance coverage, certificates of insurance (COIs), and policy terms;
- maintenance and service agreements.

The review must distinguish evidence from conclusions. A Gmail message can show
that a document or offer exists; it cannot by itself prove that insurance is
adequate, a contract is legally executed, or a service relationship is active.

## Sources and side effects

1. Read Airtable table `tblmysPS8GSncnWSa` (`All Vendors`) through view
   `viwVD8IFpH6fXUPvh` (`Every Vendor Ever`) records.
2. Read Gmail messages and relevant PDF attachments.
3. Match messages to directory vendors by business name, contact address,
   sender/original-sender headers, and URL domain.
4. Produce a local JSON report and the existing approval-gated proposal queue.
5. Never mutate Airtable during `audit` or `scan`; only `approve` may apply a
   displayed field change.

The live Airtable token and Gmail OAuth token are injected at runtime. Neither
is stored in source, report output, command arguments, or logs.

## Data model signatures

```python
classify_document(subject: str, body: str, filenames: list[str]) -> set[Category]

review_vendors(
    vendors: list[dict[str, Any]],
    messages: list[tuple[dict[str, Any], dict[str, bytes]]],
    lookback_days: int,
    history_days: int,
) -> AuditReport

VendorReviewService.audit(
    lookback_days: int,
    history_days: int | None = None,
) -> AuditReport
```

`Category` is one of `contract_terms`, `insurance`, or `maintenance`.

Each `VendorReview` contains one `ReviewFinding` per category. Findings are
`documented_recent`, `documented_older`, `possible_lead`, or `missing`. Every
non-missing finding contains message IDs, Gmail links, sender/subject/date,
attachment names, and facts. `possible_lead` is used for solicitations or
quotes and is not treated as proof of an agreement or coverage.

The report also contains `messages_scanned`, `directory_count`,
`active_directory_count`, `matched_vendor_count`, and uncatalogued Gmail
candidates. Off-boarded directory entries remain visible with `active=false`
and are not silently counted as active services.

## Call stack

```text
CLI audit
  -> Config.from_env
  -> VendorReviewService.audit
       -> AirtableClient.records
       -> GmailClient.search(history query)
       -> GmailClient.message / attachment
       -> review_vendors
            -> classify_document
            -> match_message_to_vendor
            -> summarize_findings
       -> AuditStore.save_report
  -> JSON or human report
```

`scan` uses the same source-loading path, then sends the evidence through the
existing electricity-transition detector and approval-safe ProposalStore. The
complete matrix is available from `audit`; proposals and findings are separate
so a missing COI never becomes an accidental Airtable write.

## Test slices

1. Classification identifies contract/terms, insurance, and maintenance
   evidence while ignoring ordinary invoices.
2. Insurance and maintenance solicitations become `possible_lead`, not
   documented coverage/service agreements.
3. A directory vendor with no matching evidence gets a `missing` finding in
   every required category.
4. Older evidence becomes `documented_older`; recent evidence becomes
   `documented_recent`.
5. Group-forwarded Gmail headers (`X-Original-Sender` and `Reply-To`) match the
   right vendor.
6. An external sender not in Airtable appears as an uncatalogued candidate.
7. Off-boarded vendors are represented but excluded from active counts.
8. Report persistence is atomic and JSON round-trips.
9. Existing electricity-transition and approval-gated tests remain green.

## Safety gates

- `audit` and `scan` are read-only against Airtable.
- Attachment downloads are limited to relevant document filenames/types.
- Evidence is deduplicated by message ID, category, and attachment name.
- A generic sender or keyword never creates an Airtable field update.
- Secrets are loaded from environment/token files and are never printed.
- No GitHub merge or production deployment is part of this change; a separate
  independent review is required before merging the implementation.
