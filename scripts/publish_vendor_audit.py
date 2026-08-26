#!/usr/bin/env python3
"""Validate a vendor audit report and deliver a concise summary to Slack."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

EXPECTED_CATEGORIES = {"contract_terms", "insurance", "maintenance"}
STATUSES = ("documented_recent", "documented_older", "possible_lead", "missing")
Opener = Callable[..., Any]


def _validate_report(report: dict[str, Any]) -> None:
    vendors = report.get("vendors")
    if not isinstance(vendors, list):
        raise ValueError("audit report has no vendor list")
    if report.get("directory_count") != len(vendors):
        raise ValueError("audit report directory count does not match vendor rows")
    active_count = sum(bool(vendor.get("active")) for vendor in vendors)
    if report.get("active_directory_count") != active_count:
        raise ValueError("audit report active count does not match vendor rows")

    missing_count = 0
    for vendor in vendors:
        findings = vendor.get("findings", {})
        if set(findings) != EXPECTED_CATEGORIES:
            raise ValueError(f"audit report has incomplete findings for {vendor.get('name', 'unknown vendor')}")
        for finding in findings.values():
            if finding.get("status") not in STATUSES:
                raise ValueError("audit report contains an unknown finding status")
            if vendor.get("active") and finding.get("status") == "missing":
                missing_count += 1
    if report.get("missing_count") != missing_count:
        raise ValueError("audit report missing count does not match vendor findings")


def _counts(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    result = {category: {status: 0 for status in STATUSES} for category in EXPECTED_CATEGORIES}
    for vendor in report["vendors"]:
        if not vendor.get("active"):
            continue
        for category, finding in vendor["findings"].items():
            result[category][finding["status"]] += 1
    return result


def _short_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%B %-d, %Y")
    except ValueError:
        return value[:10]


def render_report(report: dict[str, Any], run_url: str) -> str:
    counts = _counts(report)
    labels = {
        "contract_terms": "Contracts / terms",
        "insurance": "Insurance / COIs",
        "maintenance": "Maintenance / service",
    }
    lines = [
        f"*Monthly Vendor Audit — {_short_date(str(report.get('generated_at', '')))}*",
        f"Airtable directory: {report['directory_count']} total / {report['active_directory_count']} active",
        f"Gmail messages scanned: {report['messages_scanned']}",
        "",
        "*Active-vendor control results:*",
    ]
    for category in ("contract_terms", "insurance", "maintenance"):
        current = counts[category]
        lines.append(
            f"• {labels[category]}: "
            f"{current['documented_recent']} recent, "
            f"{current['documented_older']} older, "
            f"{current['possible_lead']} lead(s), "
            f"{current['missing']} missing"
        )
    lines.extend([
        "",
        f"Uncatalogued Gmail candidates: {len(report.get('uncatalogued', []))}",
        f"<{run_url}|Open the GitHub Actions run>",
        "No Airtable changes were made by this audit.",
    ])
    return "\n".join(lines)


def _slack_call(token: str, method: str, payload: dict[str, Any], opener: Opener) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with opener(request, timeout=30) as response:
            body = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeError(f"Slack {method} request failed: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} rejected the request: {body.get('error', 'unknown error')}")
    return body


def deliver_slack(text: str, token: str, recipient_user_id: str, opener: Opener = urllib.request.urlopen) -> None:
    opened = _slack_call(token, "conversations.open", {"users": recipient_user_id}, opener)
    channel_id = opened.get("channel", {}).get("id")
    if not channel_id:
        raise RuntimeError("Slack did not return a DM channel")
    _slack_call(
        token,
        "chat.postMessage",
        {"channel": channel_id, "text": text, "unfurl_links": False},
        opener,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", type=Path)
    mode.add_argument("--failure", metavar="MESSAGE")
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args(argv)

    token = os.getenv("SLACK_BOT_TOKEN", "")
    recipient = os.getenv("SLACK_RECIPIENT_USER_ID", "")
    if not token or not recipient:
        print("Missing SLACK_BOT_TOKEN or SLACK_RECIPIENT_USER_ID", file=sys.stderr)
        return 2

    if args.failure is not None:
        text = f"*Monthly Vendor Audit needs attention*\n{args.failure}\n<{args.run_url}|Open the GitHub Actions run>"
    else:
        try:
            report = json.loads(args.report.read_text())
            _validate_report(report)
            text = render_report(report, args.run_url)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Invalid audit report: {exc}", file=sys.stderr)
            return 2

    try:
        deliver_slack(text, token, recipient)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Slack delivery verified by API response")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
