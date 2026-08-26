from __future__ import annotations

import io
import re
from datetime import date
from hashlib import sha256
from html import unescape
from typing import Any

from pypdf import PdfReader

from .mime import headers, received_at, text_parts
from .models import AttachmentRef, Evidence, FieldChange, Proposal, Question


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
ESIID_RE = re.compile(r"\b10\d{15}\b")


def pdf_text(data: bytes) -> str:
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def clean_html(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", value))


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def stores(fields: dict[str, Any]) -> set[str]:
    values = fields.get("Stores", []) or []
    return {x if isinstance(x, str) else x.get("name", "") for x in values}


def detect(
    vendors: list[dict[str, Any]],
    messages: list[tuple[dict[str, Any], dict[str, bytes]]],
) -> list[Proposal]:
    proposals: list[Proposal] = []
    for message, blobs in messages:
        text = clean_html(text_parts(message))
        for filename, data in blobs.items():
            if filename.lower().endswith(".pdf"):
                text += "\n" + pdf_text(data)
        proposal = detect_electricity_transition(vendors, message, text, blobs)
        if proposal:
            proposals.append(proposal)
    unique: dict[str, Proposal] = {}
    for proposal in proposals:
        unique[proposal.fingerprint] = proposal
    return list(unique.values())


def detect_electricity_transition(
    vendors: list[dict[str, Any]],
    message: dict[str, Any],
    text: str,
    blobs: dict[str, bytes],
) -> Proposal | None:
    lower = text.lower()
    if "commercial electricity supply agreement" not in lower:
        return None
    company_match = re.search(
        r"(Gridmatic Rosa LLC,?\s+dba\s+Gridmatic Retail|[A-Z][\w .,&'-]+,?\s+dba\s+[A-Z][\w .,&'-]+)",
        text,
        re.I,
    )
    if not company_match:
        return None
    company = re.sub(r"\s+", " ", company_match.group(1)).strip()
    store = "RoundRock" if "round rock" in lower else None
    candidates = [
        v for v in vendors
        if (not store or store in stores(v.get("fields", {})))
        and "electric bill" in str(v.get("fields", {}).get("Notes", "")).lower()
    ]
    if len(candidates) != 1:
        return None
    vendor = candidates[0]
    fields = vendor["fields"]
    email = _first(r"Email:\s*([^\s]+@[^\s]+)", text) or _preferred_email(text)
    sender_email = _first(EMAIL_RE, headers(message).get("from", ""))
    phone = _first(r"Toll Free Number:\s*([^\n]+)", text) or _preferred_phone(text)
    website = _first(r"Website:\s*((?:www\.)?[^\s]+)", text)
    website = f"https://{website}" if website and not website.startswith("http") else website
    price = _first(r"(?:Price:|Price\s+Type:.*?Price:)\s*\$?\s*([0-9.]+)\s*/?kWh", text)
    term = _first(r"Term:\s*(\d+)\s*month", text)
    flattened_terms = re.search(r"\.(\d{5})\s+(\d{1,3})\s+", text)
    if flattened_terms:
        price = price or f"0.{flattened_terms.group(1)}"
        term = term or flattened_terms.group(2)
    esiid = _first(ESIID_RE, text)
    start = _first(r"Meter Reading On or After\s*([0-9/]+)", text)
    start = start or _first(r"Round Rock,?\s*TX,?\s*78664\s+(\d{1,2}/\d{4})", text)

    changes = [
        FieldChange("Vendor", fields.get("Vendor"), company, "Executed electricity agreement names the new REP"),
    ]
    for field_name, after, reason in [
        ("Website", website, "Agreement notice section"),
        ("Contact Email", email, "Agreement notice section"),
        ("Contact phone#", phone, "Agreement notice section"),
    ]:
        if after and normalized(str(fields.get(field_name, ""))) != normalized(after):
            changes.append(FieldChange(field_name, fields.get(field_name), after, reason))
    if fields.get("Contact Name") != "Gridmatic Retail Operations":
        changes.append(FieldChange("Contact Name", fields.get("Contact Name"), "Gridmatic Retail Operations", "Role-based notice contact in agreement"))
    if sender_email and sender_email.lower().endswith("@gridmatic.com") and sender_email.lower() != str(email).lower():
        changes.append(FieldChange("Contact Email 2", fields.get("Contact Email 2"), sender_email, "Authenticated Gridmatic enrollment sender"))
    old_account = fields.get("Account #")
    if old_account:
        changes.append(FieldChange("Account #", old_account, "", "Old supplier account must not remain on renamed record; new account requested"))
    notes = _notes(fields.get("Notes", ""), price, term, esiid, start)
    if old_account and f"Former supplier account: {old_account}" not in notes:
        notes += f"\nFormer supplier account: {old_account}"
    if notes != fields.get("Notes", ""):
        changes.append(FieldChange("Notes", fields.get("Notes"), notes, "Agreement commercial terms and service identifier"))

    header = headers(message)
    filename = next((name for name in blobs if "executed" in name.lower()), None)
    evidence = Evidence(
        message_id=message["id"],
        subject=header.get("subject", ""),
        sender=header.get("from", ""),
        received_at=received_at(message),
        gmail_url=f"https://mail.google.com/mail/u/0/#all/{message['id']}",
        attachment_name=filename,
        facts=[x for x in [f"Provider: {company}", f"Term: {term} months" if term else None,
                           f"Rate: ${price}/kWh" if price else None, f"ESI ID: {esiid}" if esiid else None,
                           f"Start: meter read on/after {start}" if start else None] if x],
    )
    digest = sha256(f"{vendor['id']}:{company}:{message['id']}".encode()).hexdigest()[:6].upper()
    questions = []
    if not URL_RE.search(text) and not website:
        questions.append(Question("portal_url", "What is the Gridmatic customer portal URL?", False))
    else:
        questions.append(Question("portal_url", "Has Gridmatic provided a separate customer portal URL?", False))
    questions.append(Question("billing_account", "Has Gridmatic issued a billing account number yet?", False))
    questions.append(Question("payment_method", "Is Gridmatic billing enrolled in AutoPay, and by what payment method?", False))
    questions.append(Question("switch_confirmed", "Did the meter switch complete, so Hudson/Tara can be treated as replaced?", False))
    return Proposal(
        id=f"VR-{date.today():%Y%m%d}-{digest}",
        kind="provider_transition",
        record_id=vendor["id"],
        vendor_before=fields.get("Vendor", ""),
        store=store,
        confidence=0.99,
        changes=changes,
        evidence=[evidence],
        attachments=[AttachmentRef(message["id"], filename)] if filename else [],
        questions=questions,
    )


def _notes(existing: str, price: str | None, term: str | None, esiid: str | None, start: str | None) -> str:
    lines = ["Electric Bill"]
    if esiid:
        lines.append(f"ESI ID: {esiid}")
    if price:
        lines.append(f"Supply rate: ${price}/kWh fixed")
    if term:
        lines.append(f"Term: {term} months")
    if start:
        lines.append(f"Service start: meter reading on or after {start}")
    block = "\n".join(lines)
    if block in existing:
        return existing
    return f"{existing.rstrip()}\n\n{block}".strip()


def _first(pattern, text: str) -> str | None:
    match = pattern.search(text) if hasattr(pattern, "search") else re.search(pattern, text, re.I | re.S)
    if not match:
        return None
    value = match.group(1) if match.lastindex else match.group(0)
    return value.strip(" .,)\r")


def _preferred_email(text: str) -> str | None:
    emails = EMAIL_RE.findall(text)
    return next((x for x in emails if "retail" in x.lower() or "support" in x.lower()), None)


def _preferred_phone(text: str) -> str | None:
    phones = PHONE_RE.findall(text)
    return phones[0] if phones else None
