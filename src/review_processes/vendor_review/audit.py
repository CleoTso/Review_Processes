"""Read-only vendor evidence classification and review matrix."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from html import unescape
from typing import Any, Iterable
from urllib.parse import urlparse

from .detect import pdf_text
from .mime import attachments as message_attachments
from .mime import headers, received_at, text_parts
from .models import Evidence


class Category(str, Enum):
    CONTRACT_TERMS = "contract_terms"
    INSURANCE = "insurance"
    MAINTENANCE = "maintenance"


class FindingStatus(str, Enum):
    DOCUMENTED_RECENT = "documented_recent"
    DOCUMENTED_OLDER = "documented_older"
    POSSIBLE_LEAD = "possible_lead"
    MISSING = "missing"


CATEGORIES = tuple(Category)

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# These are intentionally phrase-based.  Generic words such as "bill" and
# "service" are not enough to establish that a contract or coverage document
# exists.
_INSURANCE_TERMS = (
    r"\bcertificate\s+of\s+insurance\b",
    r"\binsurance\s+certificate\b",
    r"\bCOI\b",
    r"\badditional\s+insured\b",
    r"\bgeneral\s+liability\b",
    r"\bworkers['’]?\s+comp(?:ensation|)\b",
    r"\bcommercial\s+auto\b",
    r"\bumbrella\s+liability\b",
    r"\binsurance\s+policy\b",
    r"\bpolicy\s+(?:number|no\.?|period|term|limits?)\b",
    r"\bdeclarations\s+page\b",
    r"\bcoverage\s+(?:limit|limits|period|effective|expires?)\b",
    r"\binsurance\b",
)
_MAINTENANCE_TERMS = (
    r"\bmaintenance\b",
    r"\bHVAC\b",
    r"\brefrigeration\b",
    r"\bplumbing\b",
    r"\bpest\s+control\b",
    r"\bhood\s+(?:cleaning|service|maintenance)\b",
    r"\bgrease\s+(?:trap|interceptor)\b",
    r"\bair\s+filter(?:s)?\b",
    r"\bpreventive\s+maintenance\b",
    r"\bservice\s+call\b",
    r"\brepair\b",
)
_CONTRACT_TERMS = (
    r"\bexecuted\s+(?:commercial\s+)?(?:contract|agreement)\b",
    r"\bfully\s+executed\b",
    r"\bsigned\s+(?:contract|agreement)\b",
    r"\bmaster\s+service\s+agreement\b",
    r"\bterms\s+of\s+service\b",
    r"\bterms\s+and\s+conditions\b",
    r"\bcommercial\s+electricity\s+supply\s+agreement\b",
    r"\bcontract\s+(?:renewal|amendment|addendum|terms?)\b",
    r"\bagreement\s+(?:renewal|amendment|addendum|terms?)\b",
    r"\brenewal\s+(?:notice|terms?|agreement)\b",
    r"\brate\s+change\b",
    r"\baddendum\b",
    r"\bamendment\b",
    r"\bvendor\s+agreement\b",
    r"\bservice\s+agreement\b",
)

_SOLICITATION_TERMS = (
    r"\bget\s+a\s+(?:free\s+)?quote\b",
    r"\bno[- ]pressure\s+quote\b",
    r"\brequest(?:ing)?\b",
    r"\binquir(?:y|e)\b",
    r"\bwe\s+offer\b",
    r"\bare\s+you\s+the\s+right\s+person\b",
    r"\bplease\s+(?:call|reply|reach\s+out)\b",
    r"\bwhen\s+the\s+timing\s+is\s+right\b",
    r"\bavailable\s+to\s+provide\b",
)


def clean_text(value: str) -> str:
    """Return searchable text without HTML markup or legal boilerplate links."""
    value = re.sub(
        r"<a\b[^>]*(?:legal|terms|privacy)[^>]*>.*?</a>",
        " ",
        value or "",
        flags=re.I | re.S,
    )
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", value))).strip()


def classify_document(subject: str, body: str, filenames: Iterable[str]) -> set[Category]:
    """Classify a message/attachment bundle into review categories.

    Classification is deliberately broader than proof.  For example, an
    insurance solicitation is classified as insurance and later becomes a
    ``possible_lead`` finding, while an invoice with no document terms returns
    no category at all.
    """
    names = [str(name) for name in filenames if name]
    text = clean_text(" ".join([subject or "", body or "", *names]))
    lower = text.lower()
    categories: set[Category] = set()

    insurance = _matches_any(lower, _INSURANCE_TERMS)
    maintenance = _matches_any(lower, _MAINTENANCE_TERMS)
    # A bare "contract" in a footer or security notice is too noisy.  Accept
    # it only in the subject or a filename; body-only matches must carry a more
    # specific agreement/terms/renewal signal.
    contract = _matches_any(lower, _CONTRACT_TERMS) or _matches_any(
        clean_text(f"{subject} {' '.join(names)}"), (r"\bcontract\b",)
    )

    if insurance:
        categories.add(Category.INSURANCE)
    if maintenance:
        categories.add(Category.MAINTENANCE)
    # A maintenance agreement is reviewed under the maintenance control.  Do
    # not duplicate it as generic contract evidence unless it also contains an
    # explicit terms/renewal/amendment signal.
    explicit_contract = _matches_any(
        lower,
        (
            r"\bterms\s+of\s+service\b",
            r"\bterms\s+and\s+conditions\b",
            r"\brenewal\b",
            r"\brate\s+change\b",
            r"\bamendment\b",
            r"\baddendum\b",
            r"\bcommercial\s+electricity\s+supply\s+agreement\b",
            r"\bexecuted\s+contract\b",
            r"\bsigned\s+contract\b",
        ),
    )
    if contract and (not maintenance or explicit_contract):
        categories.add(Category.CONTRACT_TERMS)

    # An ordinary invoice or bill should not become a terms finding merely
    # because a vendor's footer says "service".
    if not categories and _matches_any(lower, (r"\binvoice\b", r"\bbill\b")):
        return set()
    return categories


@dataclass(slots=True)
class ReviewFinding:
    category: Category
    status: FindingStatus
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "status": self.status.value,
            "evidence": [asdict(item) for item in self.evidence],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReviewFinding":
        return cls(
            category=Category(value["category"]),
            status=FindingStatus(value["status"]),
            evidence=[Evidence(**item) for item in value.get("evidence", [])],
            notes=list(value.get("notes", [])),
        )


@dataclass(slots=True)
class VendorReview:
    record_id: str | None
    name: str
    active: bool
    findings: dict[Category, ReviewFinding]
    source: str = "airtable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "name": self.name,
            "active": self.active,
            "source": self.source,
            "findings": {category.value: finding.to_dict() for category, finding in self.findings.items()},
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VendorReview":
        return cls(
            record_id=value.get("record_id"),
            name=value["name"],
            active=bool(value.get("active", True)),
            source=value.get("source", "airtable"),
            findings={
                Category(key): ReviewFinding.from_dict(item)
                for key, item in value.get("findings", {}).items()
            },
        )


@dataclass(slots=True)
class UncataloguedCandidate:
    name: str
    findings: dict[Category, ReviewFinding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "findings": {category.value: finding.to_dict() for category, finding in self.findings.items()},
        }


@dataclass(slots=True)
class AuditReport:
    generated_at: str
    lookback_days: int
    history_days: int
    directory_count: int
    active_directory_count: int
    messages_scanned: int
    matched_vendor_count: int
    vendors: list[VendorReview]
    uncatalogued: list[UncataloguedCandidate] = field(default_factory=list)

    @property
    def missing_count(self) -> int:
        return sum(
            finding.status == FindingStatus.MISSING
            for vendor in self.vendors
            for finding in vendor.findings.values()
            if vendor.active
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "lookback_days": self.lookback_days,
            "history_days": self.history_days,
            "directory_count": self.directory_count,
            "active_directory_count": self.active_directory_count,
            "messages_scanned": self.messages_scanned,
            "matched_vendor_count": self.matched_vendor_count,
            "missing_count": self.missing_count,
            "vendors": [vendor.to_dict() for vendor in self.vendors],
            "uncatalogued": [candidate.to_dict() for candidate in self.uncatalogued],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuditReport":
        return cls(
            generated_at=value["generated_at"],
            lookback_days=int(value["lookback_days"]),
            history_days=int(value["history_days"]),
            directory_count=int(value["directory_count"]),
            active_directory_count=int(value["active_directory_count"]),
            messages_scanned=int(value["messages_scanned"]),
            matched_vendor_count=int(value["matched_vendor_count"]),
            vendors=[VendorReview.from_dict(item) for item in value.get("vendors", [])],
            uncatalogued=[
                UncataloguedCandidate(
                    name=item["name"],
                    findings={
                        Category(key): ReviewFinding.from_dict(finding)
                        for key, finding in item.get("findings", {}).items()
                    },
                )
                for item in value.get("uncatalogued", [])
            ],
        )


def review_vendors(
    vendors: list[dict[str, Any]],
    messages: list[tuple[Any, ...]],
    lookback_days: int,
    *,
    history_days: int | None = None,
    now: datetime | None = None,
) -> AuditReport:
    """Review every directory record against Gmail evidence.

    ``messages`` accepts ``(message, blobs)`` from the production loader and
    ``(message, blobs, body)`` for deterministic tests or alternate Gmail
    adapters.  No record is changed by this function.
    """
    now = _aware(now or datetime.now(timezone.utc))
    history_days = max(history_days or max(lookback_days, 730), lookback_days)
    recent_cutoff = now - timedelta(days=lookback_days)
    history_cutoff = now - timedelta(days=history_days)

    identities = [_identity(record) for record in vendors]
    collected: dict[tuple[int, Category], list[_EvidenceCandidate]] = {}
    uncatalogued: dict[str, list[_EvidenceCandidate]] = {}
    scanned = 0

    for item in messages:
        if len(item) < 2:
            continue
        message = item[0]
        blobs = item[1] or {}
        supplied_body = item[2] if len(item) >= 3 else None
        when = _message_datetime(message)
        if when is None or when < history_cutoff or when > now + timedelta(days=1):
            continue
        scanned += 1
        subject = headers(message).get("subject", "")
        body = clean_text(supplied_body) if supplied_body is not None else clean_text(text_parts(message))
        filenames = [str(name) for name in blobs]
        filenames.extend(item["filename"] for item in message_attachments(message) if item.get("filename"))
        for filename, data in blobs.items():
            if str(filename).lower().endswith(".pdf") and isinstance(data, (bytes, bytearray)):
                body = clean_text(f"{body} {pdf_text(bytes(data))}")
        categories = classify_document(subject, body, filenames)
        if not categories:
            continue

        vendor_index = _match_vendor(identities, message, subject, body, filenames)
        for category in categories:
            candidate = _candidate(message, subject, body, filenames, category, when)
            if vendor_index is not None:
                collected.setdefault((vendor_index, category), []).append(candidate)
            else:
                domain = _external_domain(message)
                if domain:
                    uncatalogued.setdefault(domain, []).append(candidate)

    reviews: list[VendorReview] = []
    matched_indexes: set[int] = set()
    for index, identity in enumerate(identities):
        findings: dict[Category, ReviewFinding] = {}
        for category in CATEGORIES:
            candidates = _dedupe_candidates(collected.get((index, category), []))
            finding = _finding(category, candidates, recent_cutoff)
            findings[category] = finding
            if candidates:
                matched_indexes.add(index)
        reviews.append(
            VendorReview(
                record_id=identity.record_id,
                name=identity.name,
                active=identity.active,
                findings=findings,
            )
        )

    candidates = []
    for name, evidence in sorted(uncatalogued.items()):
        findings = {
            category: _finding(category, _dedupe_candidates([item for item in evidence if item.category == category]), recent_cutoff)
            for category in CATEGORIES
        }
        if any(finding.status != FindingStatus.MISSING for finding in findings.values()):
            candidates.append(UncataloguedCandidate(name=name, findings=findings))

    return AuditReport(
        generated_at=now.isoformat(),
        lookback_days=lookback_days,
        history_days=history_days,
        directory_count=len(reviews),
        active_directory_count=sum(item.active for item in reviews),
        messages_scanned=scanned,
        matched_vendor_count=len(matched_indexes),
        vendors=reviews,
        uncatalogued=candidates,
    )


@dataclass(frozen=True, slots=True)
class _VendorIdentity:
    record_id: str | None
    name: str
    active: bool
    aliases: tuple[str, ...]
    emails: frozenset[str]
    domains: frozenset[str]


@dataclass(frozen=True, slots=True)
class _EvidenceCandidate:
    category: Category
    evidence: Evidence
    when: datetime
    strong: bool


def _finding(
    category: Category,
    candidates: list[_EvidenceCandidate],
    recent_cutoff: datetime,
) -> ReviewFinding:
    if not candidates:
        return ReviewFinding(category, FindingStatus.MISSING, notes=["No matching Gmail evidence found in the review window."])

    strong = [item for item in candidates if item.strong]
    if strong:
        status = (
            FindingStatus.DOCUMENTED_RECENT
            if any(item.when >= recent_cutoff for item in strong)
            else FindingStatus.DOCUMENTED_OLDER
        )
        notes = ["Document evidence found; legal sufficiency and coverage adequacy still require human review."]
        if any(not item.strong for item in candidates):
            notes.append("Additional messages were classified as possible leads and were not used as proof.")
    else:
        status = FindingStatus.POSSIBLE_LEAD
        notes = ["Messages mention this category, but no executed, issued, or attached document was found."]
    ordered = sorted(candidates, key=lambda item: item.when, reverse=True)
    return ReviewFinding(category, status, [item.evidence for item in ordered], notes)


def _candidate(
    message: dict[str, Any],
    subject: str,
    body: str,
    filenames: list[str],
    category: Category,
    when: datetime,
) -> _EvidenceCandidate:
    message_headers = headers(message)
    relevant = _relevant_filename(category, filenames)
    text = clean_text(f"{subject} {body} {' '.join(filenames)}")
    strong = _is_strong_evidence(category, text, _category_attachment_signal(category, filenames))
    facts = [_fact(category, strong)]
    if relevant:
        facts.append(f"Attachment: {relevant}")
    evidence = Evidence(
        message_id=str(message.get("id", "")),
        subject=subject,
        sender=message_headers.get("from", ""),
        received_at=when.isoformat(),
        gmail_url=f"https://mail.google.com/mail/u/0/#all/{message.get('id', '')}",
        attachment_name=relevant,
        facts=facts,
    )
    return _EvidenceCandidate(category, evidence, when, strong)


def _is_strong_evidence(category: Category, text: str, has_attachment: bool) -> bool:
    lower = text.lower()
    solicitation = _matches_any(lower, _SOLICITATION_TERMS)
    explicit = {
        Category.CONTRACT_TERMS: _matches_any(
            lower,
            (
                r"\bexecuted\b",
                r"\bfully\s+executed\b",
                r"\bsigned\b",
                r"\bterms\s+of\s+service\b",
                r"\bterms\s+and\s+conditions\b",
                r"\brenewal\b",
                r"\bamendment\b",
                r"\baddendum\b",
            ),
        ),
        Category.INSURANCE: has_attachment
        or _matches_any(
            lower,
            (
                r"\bcertificate\s+of\s+insurance\b",
                r"\binsurance\s+certificate\b",
                r"\badditional\s+insured\b",
                r"\bpolicy\s+(?:number|no\.?|period|term|limits?)\b",
                r"\bdeclarations\s+page\b",
                r"\bcoverage\s+(?:limit|limits|period|effective|expires?)\b",
            ),
        ),
        Category.MAINTENANCE: has_attachment
        or _matches_any(
            lower,
            (
                r"\bexecuted\b",
                r"\bfully\s+executed\b",
                r"\bsigned\b",
                r"\bactive\s+(?:service|maintenance)\b",
                r"\brecurring\s+(?:service|maintenance)\b",
                r"\bpreventive\s+maintenance\s+(?:plan|agreement|schedule)\b",
                r"\bservice\s+(?:plan|agreement|contract)\b",
            ),
        ),
    }[category]
    proof_language = _matches_any(
        lower,
        (
            r"\bexecuted\b",
            r"\bsigned\b",
            r"\bissued\b",
            r"\bcertificate\s+of\s+insurance\b",
            r"\binsurance\s+certificate\b",
            r"\badditional\s+insured\b",
            r"\bpolicy\s+(?:number|no\.?|period|term|limits?)\b",
            r"\bdeclarations\s+page\b",
        ),
    )
    if solicitation and not proof_language:
        return False
    return bool(explicit)


def _category_attachment_signal(category: Category, filenames: list[str]) -> bool:
    terms = {
        Category.CONTRACT_TERMS: ("contract", "agreement", "terms", "renewal", "amend", "addendum"),
        Category.INSURANCE: ("insurance", "coi", "policy", "certificate", "declaration"),
        Category.MAINTENANCE: ("maintenance", "service", "hvac", "filter", "repair", "grease", "pest", "hood"),
    }[category]
    return any(any(term in str(filename).lower() for term in terms) for filename in filenames)


def _fact(category: Category, strong: bool) -> str:
    if strong:
        return {
            Category.CONTRACT_TERMS: "Contract or terms evidence",
            Category.INSURANCE: "Insurance or COI evidence",
            Category.MAINTENANCE: "Maintenance or service-agreement evidence",
        }[category]
    return {
        Category.CONTRACT_TERMS: "Contract or terms lead",
        Category.INSURANCE: "Insurance or COI lead",
        Category.MAINTENANCE: "Maintenance or service lead",
    }[category]


def _identity(record: dict[str, Any]) -> _VendorIdentity:
    fields = record.get("fields", {}) or {}
    name = _first_value(fields, ("Name", "Vendor", "Company", "Provider")) or "Unnamed vendor"
    aliases = set(_name_aliases(name))
    emails: set[str] = set()
    domains: set[str] = set()
    for key, value in fields.items():
        raw = " ".join(_flatten(value))
        emails.update(item.lower() for item in _EMAIL_RE.findall(raw))
        if key.lower() in {"url", "website", "portal", "web site"}:
            for url in _URL_RE.findall(raw):
                domain = _domain(url)
                if domain:
                    domains.add(domain)
    domains.update(_domain(email) for email in emails if _domain(email))
    status_values = " ".join(_flatten(fields.get("Current Status") or fields.get("Status", ""))).lower()
    active = not bool(re.search(r"\boff\s*boarded\b|\binactive\b|\bdiscontinued\b", status_values))
    return _VendorIdentity(
        record_id=record.get("id"),
        name=str(name).strip(),
        active=active,
        aliases=tuple(sorted(alias for alias in aliases if len(alias) >= 5)),
        emails=frozenset(emails),
        domains=frozenset(domain for domain in domains if domain),
    )


def _match_vendor(
    identities: list[_VendorIdentity],
    message: dict[str, Any],
    subject: str,
    body: str,
    filenames: list[str],
) -> int | None:
    sender_values = _sender_values(message)
    sender_emails = {email.lower() for value in sender_values for email in _EMAIL_RE.findall(value)}
    sender_domains = {domain for email in sender_emails if (domain := _domain(email))}
    searchable = _normalized(" ".join([subject, body, *filenames, *sender_values]))

    scored: list[tuple[int, int]] = []
    for index, identity in enumerate(identities):
        score = 0
        if sender_emails & identity.emails:
            score += 100
        if any(_domain_matches(left, right) for left in sender_domains for right in identity.domains):
            score += 40
        if any(alias in searchable for alias in identity.aliases):
            score += 20
        if score:
            scored.append((score, index))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _external_domain(message: dict[str, Any]) -> str | None:
    internal = {"tsochinese.com", "github.com", "google.com", "slack.com"}
    values = _sender_values(message)
    emails = [email.lower() for value in values for email in _EMAIL_RE.findall(value)]
    for email in emails:
        domain = _domain(email)
        if domain and not any(domain == item or domain.endswith("." + item) for item in internal):
            return domain
    return None


def _sender_values(message: dict[str, Any]) -> list[str]:
    wanted = {"from", "sender", "reply-to", "x-original-sender", "x-original-from"}
    return [
        item.get("value", "")
        for item in message.get("payload", {}).get("headers", [])
        if item.get("name", "").lower() in wanted
    ]


def _message_datetime(message: dict[str, Any]) -> datetime | None:
    # Gmail's internalDate is the received timestamp and is more reliable than
    # a sender-controlled Date header.  It also makes forwarded messages sort
    # correctly.
    raw = message.get("internalDate")
    if raw not in (None, ""):
        try:
            value = float(raw)
            if value > 100_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    value = headers(message).get("date")
    if value:
        try:
            return _aware(parsedate_to_datetime(value))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _relevant_filename(category: Category, filenames: list[str]) -> str | None:
    terms = {
        Category.CONTRACT_TERMS: ("contract", "agreement", "terms", "renewal", "amend", "addendum"),
        Category.INSURANCE: ("insurance", "coi", "policy", "certificate", "declaration"),
        Category.MAINTENANCE: ("maintenance", "service", "hvac", "filter", "repair", "grease", "pest", "hood"),
    }[category]
    for filename in filenames:
        lower = filename.lower()
        if any(term in lower for term in terms):
            return filename
    return next((filename for filename in filenames if filename.lower().endswith((".pdf", ".doc", ".docx"))), None)


def _dedupe_candidates(candidates: list[_EvidenceCandidate]) -> list[_EvidenceCandidate]:
    seen: set[tuple[str, str | None]] = set()
    output: list[_EvidenceCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.when, reverse=True):
        key = (candidate.evidence.message_id, candidate.evidence.attachment_name)
        if key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def _name_aliases(name: str) -> set[str]:
    raw = str(name).strip()
    aliases = {_normalized(raw)}
    without_parenthetical = re.sub(r"\([^)]*\)", " ", raw)
    aliases.add(_normalized(without_parenthetical))
    for acronym in re.findall(r"\(([^)]{2,12})\)", raw):
        aliases.add(_normalized(acronym))
    return {alias for alias in aliases if alias}


def _first_value(fields: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for actual, value in fields.items():
            if actual.lower().strip() == key.lower().strip() and value not in (None, "", []):
                values = _flatten(value)
                if values:
                    return values[0]
    return None


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten(item))
        return result
    if isinstance(value, dict):
        return _flatten(value.get("name") or value.get("url") or value.get("value"))
    return [str(value)]


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _domain(value: str) -> str | None:
    raw = value.lower().strip()
    if "@" in raw and not raw.startswith(("http://", "https://")):
        raw = raw.rsplit("@", 1)[1]
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    return (parsed.hostname or "").lower().strip(".") or None


def _domain_matches(left: str, right: str) -> bool:
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


__all__ = [
    "AuditReport",
    "Category",
    "FindingStatus",
    "ReviewFinding",
    "UncataloguedCandidate",
    "VendorReview",
    "classify_document",
    "clean_text",
    "review_vendors",
]
