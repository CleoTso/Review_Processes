import base64
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from review_processes.vendor_review.audit import (
    Category,
    FindingStatus,
    classify_document,
    review_vendors,
)


def message(message_id, subject, body, sender="vendor@example.com", received="2026-08-20T12:00:00+00:00"):
    return (
        {
            "id": message_id,
            "internalDate": str(int(datetime.fromisoformat(received).timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                    {"name": "Date", "value": "Thu, 20 Aug 2026 12:00:00 +0000"},
                ]
            },
        },
        {},
        body,
    )


class ClassificationTests(unittest.TestCase):
    def test_classifies_each_review_category(self):
        self.assertEqual(
            classify_document("Executed Service Agreement", "contract term renewal", ["agreement.pdf"]),
            {Category.CONTRACT_TERMS},
        )
        self.assertEqual(
            classify_document("Certificate of Insurance", "additional insured general liability", ["COI.pdf"]),
            {Category.INSURANCE},
        )
        self.assertEqual(
            classify_document("Preventive Maintenance Agreement", "HVAC service schedule", ["maintenance.pdf"]),
            {Category.MAINTENANCE},
        )

    def test_invoice_alone_is_not_a_contract_finding(self):
        self.assertEqual(classify_document("Invoice #123", "monthly invoice amount due", ["invoice.pdf"]), set())

    def test_body_only_contract_word_is_not_enough(self):
        self.assertEqual(classify_document("Password verification code", "Your contract code is 1234", []), set())

    def test_legal_footer_link_is_not_updated_terms_evidence(self):
        footer = '<a href="https://vendor.example/legal/terms">Terms of service</a> | <a href="https://vendor.example/privacy">Privacy</a>'
        self.assertEqual(classify_document("Password verification code", footer, []), set())

    def test_attached_insurance_quote_is_not_issued_coverage(self):
        self.assertEqual(
            classify_document("Insurance quote for Tso", "We offer a quote; please reply for coverage", ["insurance-quote.docx"]),
            {Category.INSURANCE},
        )
        msg = message("m1", "Insurance quote for Tso", "We offer a quote; please reply for coverage", "sales@broker.example.com")
        report = review_vendors(
            [{"id": "v1", "fields": {"Name": "Broker", "Email 1": "sales@broker.example.com"}}],
            [(msg[0], {"insurance-quote.docx": b"not-a-real-docx"}, msg[2])],
            lookback_days=90,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(report.vendors[0].findings[Category.INSURANCE].status, FindingStatus.POSSIBLE_LEAD)


class ReviewTests(unittest.TestCase):
    def test_solicitations_are_leads_not_proof(self):
        vendors = [{"id": "v1", "fields": {"Name": "Ned Air Filters"}}]
        messages = [message("m1", "AC Filter Maintenance for Tso Chinese", "We offer scheduled reminders and delivery. Get a quote.", "sales@nedairfilters.com")]
        report = review_vendors(vendors, messages, lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        review = report.vendors[0]
        self.assertEqual(review.findings[Category.MAINTENANCE].status, FindingStatus.POSSIBLE_LEAD)
        self.assertEqual(review.findings[Category.INSURANCE].status, FindingStatus.MISSING)

    def test_unattached_certificate_request_is_a_lead_not_documented_recent(self):
        vendors = [{"id": "v1", "fields": {"Name": "Example Vendor", "Email 1": "vendor@example.com"}}]
        msg = message("request", "Request: please provide a certificate of insurance", "")
        self.assertEqual(msg[1], {})  # exact synthetic regression: no attachments
        report = review_vendors(
            vendors, [msg], lookback_days=90,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        finding = report.vendors[0].findings[Category.INSURANCE]
        self.assertEqual(finding.status, FindingStatus.POSSIBLE_LEAD)
        self.assertIsNone(finding.evidence[0].attachment_name)
        self.assertEqual(finding.evidence[0].facts, ["Insurance or COI lead"])

    def test_requested_or_negated_proof_words_do_not_establish_evidence(self):
        vendors = [{"id": "v1", "fields": {"Name": "Example Vendor", "Email 1": "vendor@example.com"}}]
        cases = [
            ("Request", "please provide an issued certificate of insurance"),
            ("Certificate of Insurance", "Please send the insurance certificate."),
            ("Certificate of Insurance", "The certificate has not been issued."),
            ("Insurance", "We have no certificate of insurance."),
            ("Requested certificate of insurance", "We don't have the certificate yet."),
            ("Request", "Please provide a signed contract."),
        ]
        for subject, body in cases:
            with self.subTest(subject=subject, body=body):
                category = Category.CONTRACT_TERMS if "contract" in body else Category.INSURANCE
                report = review_vendors(
                    vendors, [message("request", subject, body)], lookback_days=90,
                    now=datetime(2026, 8, 26, tzinfo=timezone.utc),
                )
                self.assertEqual(report.vendors[0].findings[category].status, FindingStatus.POSSIBLE_LEAD)

    def test_supplied_certificate_and_legitimate_attachment_remain_proof(self):
        vendors = [{"id": "v1", "fields": {"Name": "Example Vendor", "Email 1": "vendor@example.com"}}]
        attached = message("attached", "Request: please provide a certificate of insurance", "")
        cases = [
            message("issued", "Certificate of Insurance", "Your certificate of insurance was issued today."),
            message("certificate", "Certificate of Insurance", "additional insured general liability"),
            message("policy", "Insurance", "Policy No. 123. Coverage effective today."),
            message("supplied", "COI request", "We provided the certificate of insurance. Please reply with questions."),
            (attached[0], {"COI.docx": b"synthetic-document"}, attached[2]),
        ]
        for msg in cases:
            with self.subTest(message_id=msg[0]["id"]):
                report = review_vendors(
                    vendors, [msg], lookback_days=90,
                    now=datetime(2026, 8, 26, tzinfo=timezone.utc),
                )
                finding = report.vendors[0].findings[Category.INSURANCE]
                self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                self.assertEqual(finding.evidence[0].facts[0], "Insurance or COI evidence")
                if msg[0]["id"] == "attached":
                    self.assertEqual(finding.evidence[0].attachment_name, "COI.docx")

    def test_missing_categories_are_reported_for_every_vendor(self):
        vendors = [{"id": "v1", "fields": {"Name": "ACME Services"}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(set(report.vendors[0].findings), set(Category))
        self.assertTrue(all(f.status == FindingStatus.MISSING for f in report.vendors[0].findings.values()))

    def test_recent_and_old_evidence_are_distinguished(self):
        vendors = [{"id": "v1", "fields": {"Name": "ACME Services", "Email 1": "vendor@example.com"}}]
        messages = [
            message("old", "Service Agreement", "executed agreement term", received="2025-01-01T12:00:00+00:00"),
            message("new", "Service Agreement", "executed agreement term", received="2026-08-01T12:00:00+00:00"),
        ]
        report = review_vendors(vendors, messages, lookback_days=90, history_days=730, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        finding = report.vendors[0].findings[Category.CONTRACT_TERMS]
        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
        self.assertEqual([e.message_id for e in finding.evidence], ["new", "old"])

    def test_group_forwarded_original_sender_matches_vendor(self):
        vendors = [{"id": "v1", "fields": {"Name": "Cool HVAC", "Email 1": "service@coolhvac.com"}}]
        msg, blobs, body = message("m1", "Maintenance Agreement", "executed maintenance agreement", "catering@tsochinese.com")
        msg["payload"]["headers"].append({"name": "X-Original-Sender", "value": "service@coolhvac.com"})
        report = review_vendors(vendors, [(msg, blobs, body)], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(report.vendors[0].findings[Category.MAINTENANCE].status, FindingStatus.DOCUMENTED_RECENT)

    def test_uncatalogued_candidate_is_retained(self):
        msg = message("m1", "Executed Service Agreement", "executed contract agreement", "contracts@newvendor.com")
        report = review_vendors([], [msg], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(len(report.uncatalogued), 1)
        self.assertEqual(report.uncatalogued[0].name, "newvendor.com")

    def test_incomplete_gmail_header_does_not_abort_review(self):
        msg = message("m1", "Executed Service Agreement", "executed contract agreement", "contracts@newvendor.com")[0]
        msg["payload"]["headers"].append({"name": "X-Malformed"})
        report = review_vendors([{"id": "v1", "fields": {"Name": "newvendor.com"}}], [(msg, {}, "executed contract agreement")], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(report.messages_scanned, 1)

    def test_offboarded_vendor_is_visible_but_not_active(self):
        vendors = [{"id": "v1", "fields": {"Name": "Former Vendor", "Status": ["Off Boarded"]}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertFalse(report.vendors[0].active)
        self.assertEqual(report.active_directory_count, 0)

    def test_current_status_is_used_for_active_directory_count(self):
        vendors = [{"id": "v1", "fields": {"Vendor": "Former Vendor", "Current Status": ["Inactive"]}}]
        report = review_vendors(vendors, [], lookback_days=90, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertFalse(report.vendors[0].active)
        self.assertEqual(report.active_directory_count, 0)


class ProofRegressionTests(unittest.TestCase):
    def finding(self, category, subject, body, filenames=(), *, mime_type=None, pdf_content=""):
        msg = message("regression", subject, body)
        blobs = {name: b"synthetic-document" for name in filenames}
        if mime_type:
            # Exercise the production text_parts path as well as supplied bodies.
            msg[0]["payload"].update({
                "mimeType": mime_type,
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            })
            bundle = (msg[0], blobs)
        else:
            bundle = (msg[0], blobs, body)
        with patch("review_processes.vendor_review.audit.pdf_text", return_value=pdf_content):
            report = review_vendors(
                [{"id": "v1", "fields": {"Name": "Example Vendor", "Email 1": "vendor@example.com"}}],
                [bundle], lookback_days=90,
                now=datetime(2026, 8, 26, tzinfo=timezone.utc),
            )
        return report.vendors[0].findings[category]

    def assert_lead_without_proof_attachment(self, finding):
        self.assertEqual(finding.status, FindingStatus.POSSIBLE_LEAD)
        self.assertIsNone(finding.evidence[0].attachment_name)
        self.assertEqual(len(finding.evidence[0].facts), 1)
        self.assertTrue(finding.evidence[0].facts[0].endswith("lead"))

    def test_insurance_offer_and_brochure_are_not_coverage(self):
        finding = self.finding(
            Category.INSURANCE, "Insurance quote", "We offer insurance. Get a free quote.",
            ["insurance-brochure.docx"],
        )
        self.assert_lead_without_proof_attachment(finding)

    def test_requested_missing_and_negated_documents_across_categories(self):
        cases = [
            (Category.CONTRACT_TERMS, "Signed contract", "Please provide a fully executed contract."),
            (Category.CONTRACT_TERMS, "Signed contract", "The signed contract has not been supplied."),
            (Category.CONTRACT_TERMS, "Contract", "As requested, please provide a signed agreement."),
            (Category.INSURANCE, "Certificate of insurance", "Please provide an issued certificate of insurance."),
            (Category.INSURANCE, "Certificate of insurance", "The certificate has not been issued."),
            (Category.INSURANCE, "Insurance", "Coverage effective dates are missing."),
            (Category.MAINTENANCE, "Preventive maintenance plan", "Please send the signed maintenance agreement."),
            (Category.MAINTENANCE, "Preventive maintenance plan", "The maintenance agreement has not been signed."),
            (Category.MAINTENANCE, "Maintenance", "We have no active maintenance plan."),
        ]
        for category, subject, body in cases:
            with self.subTest(category=category, body=body):
                self.assert_lead_without_proof_attachment(self.finding(category, subject, body))

    def test_request_quote_and_brochure_files_are_not_proof_or_proof_facts(self):
        cases = [
            (Category.CONTRACT_TERMS, "Signed contract", ["contract-quote.pdf", "agreement_request.docx", "agreement-brochure.pdf", "contract-not-signed.pdf"], "The signed contract is enclosed"),
            (Category.INSURANCE, "Certificate of insurance", ["insurance-quote.docx", "COI-request.pdf", "policy-brochure.pdf", "COI-requests.pdf"], "Coverage effective 2026-08-01"),
            (Category.MAINTENANCE, "Preventive maintenance plan", ["maintenance-quote.pdf", "maintenance_request.docx", "service-agreement-brochure.docx", "maintenance-proposals.docx"], "The signed maintenance agreement is enclosed"),
        ]
        for category, subject, filenames, pdf_content in cases:
            for filename in filenames:
                for body in ("", "Please provide the document."):
                    with self.subTest(category=category, filename=filename, body=body):
                        # Even a proof-like PDF heading must not be imported from a request/quote file.
                        finding = self.finding(category, subject, body, [filename], pdf_content=pdf_content)
                        self.assert_lead_without_proof_attachment(finding)

    def test_quote_pdf_contents_still_discover_category_but_never_supply_proof(self):
        cases = [
            (Category.CONTRACT_TERMS, "The signed contract is enclosed"),
            (Category.INSURANCE, "Coverage effective 2026-08-01"),
            (Category.MAINTENANCE, "The signed maintenance agreement is enclosed"),
        ]
        for category, pdf_content in cases:
            with self.subTest(category=category):
                finding = self.finding(category, "Quote", "See attachment", ["quote.pdf"], pdf_content=pdf_content)
                self.assert_lead_without_proof_attachment(finding)

    def test_generic_insurance_and_service_files_cannot_override_requests(self):
        for category, subject, body, filename in [
            (Category.INSURANCE, "Insurance", "Please send a certificate of insurance.", "insurance.docx"),
            (Category.MAINTENANCE, "Maintenance", "Please send a signed maintenance agreement.", "hvac.pdf"),
            (Category.MAINTENANCE, "Maintenance", "Please send a signed maintenance agreement.", "service.docx"),
        ]:
            with self.subTest(filename=filename):
                self.assert_lead_without_proof_attachment(self.finding(category, subject, body, [filename]))

    def test_modal_document_requests_are_leads_across_categories(self):
        for category, subject in [(Category.INSURANCE, "Certificate of Insurance"),
                                  (Category.CONTRACT_TERMS, "Signed contract"),
                                  (Category.MAINTENANCE, "Signed maintenance agreement")]:
            for modal in ["Can", "Could", "Would", "Will"]:
                with self.subTest(category=category, modal=modal):
                    body = f"{modal} you please send a {subject.lower()}?"
                    self.assert_lead_without_proof_attachment(self.finding(category, subject, body))

    def test_affirmative_requested_document_modifiers_remain_proof(self):
        for body in ["Attached is the certificate you requested.",
                     "Per your request, the certificate of insurance is enclosed.",
                     "As per your request, the COI is enclosed.",
                     "We provided the requested certificate of insurance.",
                     "We provided your requested certificate of insurance.",
                     "As required, the certificate of insurance is enclosed.",
                     "We do not have a vendor in Fresno. The certificate of insurance is enclosed."]:
            with self.subTest(body=body):
                finding = self.finding(Category.INSURANCE, "Certificate of Insurance", body)
                self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)

    def test_attachment_requests_and_separate_enclosures(self):
        for category, subject in [(Category.INSURANCE, "Certificate of Insurance"),
                                  (Category.CONTRACT_TERMS, "Signed contract"),
                                  (Category.MAINTENANCE, "Signed maintenance agreement")]:
            for verb in ["attach", "upload", "forward"]:
                for prefix in ["Could you please", "Could you", "Please", "Kindly", ""]:
                    body = f"{prefix} {verb} a {subject.lower()}?".strip()
                    with self.subTest(body=body):
                        self.assert_lead_without_proof_attachment(self.finding(category, subject, body))
            for verb in ["forward", "attach", "upload"]:
                for prefix in ["could you", "please", "kindly", ""]:
                    request = f"{prefix} {verb} it to your team".strip()
                    supplied = f"The {subject.lower()} is enclosed, {request}."
                    with self.subTest(category=category, supplied=supplied):
                        self.assertEqual(self.finding(category, subject, supplied).status,
                                         FindingStatus.DOCUMENTED_RECENT)
                        conditional = f"If the {subject.lower()} is enclosed, {request}?"
                        self.assert_lead_without_proof_attachment(
                            self.finding(category, subject, conditional))

    def test_conditional_and_interrogative_supply_fragments_are_not_proof(self):
        for category, subject, filename in [
            (Category.INSURANCE, "Certificate of insurance", "COI.docx"),
            (Category.CONTRACT_TERMS, "Signed contract", "agreement.pdf"),
            (Category.MAINTENANCE, "Signed maintenance agreement", "maintenance.pdf"),
        ]:
            for fragment in [
                f"If the {subject.lower()} is enclosed",
                f"Unless the {subject.lower()} is enclosed",
                f"Assuming the {subject.lower()} is enclosed",
                f"Is the {subject.lower()} enclosed",
                f"Has the {subject.lower()} been supplied",
                f"Could the {subject.lower()} be enclosed",
            ]:
                for request in ["could you forward it to me?", "please attach it.",
                                "kindly upload it.", "forward it."]:
                    body = f"{fragment}, {request}"
                    with self.subTest(category=category, body=body):
                        self.assert_lead_without_proof_attachment(self.finding(category, subject, body))
                        # Recognizable document attachments remain authoritative.
                        attached = self.finding(category, subject, body, [filename])
                        self.assertEqual(attached.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertEqual(attached.evidence[0].attachment_name, filename)
            body = f"The {subject.lower()} is enclosed?"
            self.assert_lead_without_proof_attachment(self.finding(category, subject, body))
            self.assert_lead_without_proof_attachment(self.finding(category, subject, ""))

    def test_as_requested_affirmative_supply_with_and_without_documents(self):
        cases = [
            (Category.CONTRACT_TERMS, "Contract", "As requested, signed agreement is enclosed.", "agreement.pdf"),
            (Category.CONTRACT_TERMS, "Contract", "As requested, the signed agreement is enclosed.", "agreement.pdf"),
            (Category.INSURANCE, "Insurance", "As requested, the insurance certificate is enclosed.", "COI.docx"),
            (Category.MAINTENANCE, "Maintenance", "As requested, the signed maintenance agreement is enclosed.", "maintenance.pdf"),
        ]
        for category, subject, body, filename in cases:
            for filenames in ([], [filename]):
                with self.subTest(category=category, body=body, filenames=filenames):
                    finding = self.finding(category, subject, body, filenames)
                    self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                    self.assertEqual(finding.evidence[0].attachment_name, filename if filenames else None)

    def test_actual_proof_attachments_override_requests_and_skip_request_file_presentation(self):
        cases = [
            (Category.CONTRACT_TERMS, "Contract", "Please send a signed contract.", ["contract-request.pdf", "agreement.pdf"]),
            (Category.INSURANCE, "Insurance", "Please provide a certificate of insurance.", ["COI-request.pdf", "COI.docx"]),
            (Category.MAINTENANCE, "Maintenance", "Please send a signed maintenance agreement.", ["maintenance-request.pdf", "maintenance.pdf"]),
        ]
        for category, subject, body, filenames in cases:
            with self.subTest(category=category):
                finding = self.finding(category, subject, body, filenames)
                self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                self.assertEqual(finding.evidence[0].attachment_name, filenames[1])
                self.assertEqual(finding.evidence[0].facts[-1], f"Attachment: {filenames[1]}")
                self.assertNotIn(filenames[0], " ".join(finding.evidence[0].facts))

    def test_plain_text_newlines_and_html_blocks_preserve_supplied_proof(self):
        cases = [
            (Category.INSURANCE, "Insurance", "We issued the certificate of insurance"),
            (Category.CONTRACT_TERMS, "Contract", "The signed agreement is enclosed"),
            (Category.MAINTENANCE, "Maintenance", "The signed maintenance agreement is enclosed"),
        ]
        for category, subject, statement in cases:
            bodies = [
                (f"{statement}\nPlease reply with questions", "text/plain"),
                (f"<p>{statement}</p><p>Please reply with questions</p>", "text/html"),
                (f"<div>{statement}</div><div>Please reply with questions</div>", "text/html"),
                (f"{statement}<br>Please reply with questions", "text/html"),
            ]
            for body, mime_type in bodies:
                for source in (None, mime_type):
                    with self.subTest(category=category, body=body, source=source):
                        finding = self.finding(category, subject, body, mime_type=source)
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertIsNone(finding.evidence[0].attachment_name)

    def test_coverage_limitations_and_unrelated_disclaimers_do_not_erase_proof(self):
        bodies = [
            "Coverage effective 2026-08-01, not including prior acts.",
            "Coverage effective 2026-08-01 not including prior acts.",
            "Coverage period through 2027. We do not offer umbrella coverage.",
            "Coverage period through 2027\nWe do not offer umbrella coverage",
            "<p>Coverage period through 2027</p><p>We do not offer umbrella coverage</p>",
            "Policy No. 123. We do not offer umbrella coverage.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                finding = self.finding(Category.INSURANCE, "Certificate of insurance effective 2026-08-01", body)
                self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)

    def test_subject_only_coverage_cannot_supply_proof(self):
        for body in ["", "Thanks.", "We do not offer umbrella coverage."]:
            finding = self.finding(
                Category.INSURANCE, "Coverage effective 2026-08-01", body,
            )
            self.assert_lead_without_proof_attachment(finding)

    def test_genuine_coverage_does_not_present_a_brochure_as_its_proof_attachment(self):
        finding = self.finding(
            Category.INSURANCE, "Insurance", "Coverage effective 2026-08-01",
            ["insurance-brochure.docx"],
        )
        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
        self.assertIsNone(finding.evidence[0].attachment_name)
        self.assertEqual(finding.evidence[0].facts, ["Insurance or COI evidence"])

    def test_clean_subject_cannot_borrow_unrelated_body_proof(self):
        documents = [
            (Category.CONTRACT_TERMS, "Signed contract"),
            (Category.INSURANCE, "Certificate of insurance"),
            (Category.MAINTENANCE, "Signed maintenance agreement"),
        ]
        for category, subject in documents:
            for body in ["The invoice is attached.", "We supplied the invoice."]:
                for filenames in [(), ("invoice.pdf",)]:
                    with self.subTest(category=category, body=body, filenames=filenames):
                        self.assert_lead_without_proof_attachment(
                            self.finding(category, subject, body, filenames))
            for supplied_category, document in documents:
                body = f"The {document.lower()} is enclosed."
                with self.subTest(category=category, supplied_category=supplied_category):
                    finding = self.finding(category, subject, body)
                    if category == supplied_category:
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertIsNone(finding.evidence[0].attachment_name)
                        self.assertTrue(finding.evidence[0].facts[0].endswith("evidence"))
                    else:
                        self.assert_lead_without_proof_attachment(finding)

    def test_body_document_titles_without_assertion_or_details_are_leads(self):
        for category, subject, body in [
            (Category.CONTRACT_TERMS, "Signed contract", "Service agreement"),
            (Category.INSURANCE, "Certificate of insurance", "Certificate of insurance"),
            (Category.MAINTENANCE, "Maintenance", "Preventive maintenance plan"),
        ]:
            with self.subTest(category=category):
                self.assert_lead_without_proof_attachment(self.finding(category, subject, body))

    def test_future_maintenance_without_document_supply_is_a_lead(self):
        bodies = [
            "We plan to start recurring maintenance next month.",
            "Recurring maintenance will begin in October.",
            "We are planning recurring maintenance.",
            "We expect recurring maintenance.",
            "We intend to arrange active maintenance.",
            "Upcoming recurring maintenance starts next week.",
            "We plan to start recurring maintenance next month. The invoice is attached.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assert_lead_without_proof_attachment(
                    self.finding(Category.MAINTENANCE, "Maintenance", body))
                # Relevant actual documents remain authoritative even here.
                attached = self.finding(Category.MAINTENANCE, "Maintenance", body, ["maintenance.pdf"])
                self.assertEqual(attached.status, FindingStatus.DOCUMENTED_RECENT)
                self.assertEqual(attached.evidence[0].attachment_name, "maintenance.pdf")

    def test_other_category_proof_cannot_rescue_future_maintenance(self):
        planned = "We plan to start recurring maintenance next month."
        for body, filenames in [
            (planned + " Policy No. 123. Coverage effective 2026-08-01.", ()),
            (planned, ("COI.docx",)),
        ]:
            with self.subTest(body=body, filenames=filenames):
                self.assert_lead_without_proof_attachment(
                    self.finding(Category.MAINTENANCE, "Maintenance", body, filenames))
                insurance = self.finding(Category.INSURANCE, "Maintenance", body, filenames)
                self.assertEqual(insurance.status, FindingStatus.DOCUMENTED_RECENT)

    def test_established_service_and_documents_for_future_service_remain_proof(self):
        for body in [
            "We have active maintenance with monthly visits.",
            "We have recurring maintenance with monthly visits.",
            "We plan to start recurring maintenance next month. The signed maintenance agreement is enclosed.",
            "The signed maintenance agreement is enclosed for recurring maintenance starting next month.",
            "The signed maintenance agreement covers recurring maintenance starting next month.",
        ]:
            with self.subTest(body=body):
                finding = self.finding(Category.MAINTENANCE, "Maintenance", body)
                self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                self.assertIsNone(finding.evidence[0].attachment_name)

    def test_wrapped_requests_conditions_questions_and_denials_preserve_scope(self):
        for category, subject, filename in [
            (Category.CONTRACT_TERMS, "Signed contract", "agreement.pdf"),
            (Category.INSURANCE, "Certificate of insurance", "COI.docx"),
            (Category.MAINTENANCE, "Signed maintenance agreement", "maintenance.pdf"),
        ]:
            document = subject.lower()
            templates = [
                f"Please confirm whether\nthe {document} is enclosed.",
                f"If\nthe {document} is enclosed, please forward it.",
                f"Is\nthe {document} enclosed",
                f"The {document} is enclosed\n?",
                f"The {document} is\nnot enclosed.",
            ]
            for template in templates:
                bodies = [
                    (template, "text/plain"),
                    (template.replace("\n", "<br>"), "text/html"),
                    ("<p>" + template.replace("\n", "</p><p>") + "</p>", "text/html"),
                    ("<div>" + template.replace("\n", "</div><div>") + "</div>", "text/html"),
                ]
                for body, mime_type in bodies:
                    for source in (None, mime_type):
                        with self.subTest(category=category, body=body, source=source):
                            self.assert_lead_without_proof_attachment(
                                self.finding(category, subject, body, mime_type=source))
                    with self.subTest(category=category, body=body, attached=True):
                        finding = self.finding(category, subject, body, [filename], mime_type=mime_type)
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertEqual(finding.evidence[0].attachment_name, filename)

    def test_wrapped_affirmative_enclosures_and_forwarding_remain_proof(self):
        for category, subject in [
            (Category.CONTRACT_TERMS, "Signed contract"),
            (Category.INSURANCE, "Certificate of insurance"),
            (Category.MAINTENANCE, "Signed maintenance agreement"),
        ]:
            template = f"As requested,\nthe {subject.lower()}\nis enclosed, could\nyou please forward it?"
            bodies = [
                (template, "text/plain"),
                (template.replace("\n", "<br>"), "text/html"),
                ("<p>" + template.replace("\n", "</p><p>") + "</p>", "text/html"),
                ("<div>" + template.replace("\n", "</div><div>") + "</div>", "text/html"),
            ]
            for body, mime_type in bodies:
                for source in (None, mime_type):
                    with self.subTest(category=category, body=body, source=source):
                        finding = self.finding(category, subject, body, mime_type=source)
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertIsNone(finding.evidence[0].attachment_name)

    def test_wrapped_policy_details_remain_proof(self):
        for template in ["Policy No.\n123.", "Coverage effective\n2026-08-01, not including prior acts."]:
            for body, mime_type in [
                (template, "text/plain"),
                (template.replace("\n", "<br>"), "text/html"),
                ("<p>" + template.replace("\n", "</p><p>") + "</p>", "text/html"),
            ]:
                with self.subTest(body=body, mime_type=mime_type):
                    finding = self.finding(Category.INSURANCE, "Insurance", body, mime_type=mime_type)
                    self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                    self.assertIsNone(finding.evidence[0].attachment_name)

    def test_policy_and_maintenance_invoice_filenames_are_not_proof_or_presented_attachments(self):
        cases = [
            (Category.INSURANCE, "Certificate of Insurance", "The invoice for the insurance policy is attached.", "insurance-policy-invoice.pdf"),
            (Category.INSURANCE, "Insurance", "The billing statement for the policy is attached.", "policy_billing.docx"),
            (Category.MAINTENANCE, "Maintenance", "The invoice for the maintenance agreement is attached.", "maintenance-invoice.pdf"),
            (Category.MAINTENANCE, "Maintenance", "We plan to start recurring maintenance next month. The invoice is attached.", "maintenance_billing.doc"),
        ]
        for category, subject, body, filename in cases:
            with self.subTest(filename=filename):
                # Even extracted proof-like headings in a labelled invoice cannot
                # supply proof; the filename policy governs presentation too.
                self.assert_lead_without_proof_attachment(self.finding(
                    category, subject, body, [filename],
                    pdf_content="The insurance policy is enclosed. The signed maintenance agreement is enclosed.",
                ))

    def test_actual_documents_survive_invoice_filter_and_supported_document_extensions(self):
        for extension in ("pdf", "doc", "docx"):
            for category, subject, invoice, document in [
                (Category.INSURANCE, "Insurance", "policy-invoice.pdf", "policy"),
                (Category.INSURANCE, "COI", "COI-billing.docx", "COI"),
                (Category.MAINTENANCE, "Maintenance", "maintenance-invoice.pdf", "maintenance-agreement"),
                (Category.CONTRACT_TERMS, "Signed contract", "contract-invoice.pdf", "signed-agreement"),
            ]:
                filename = f"{document}.{extension}"
                for filenames in ([filename], [invoice, filename]):
                    with self.subTest(category=category, filenames=filenames):
                        finding = self.finding(category, subject, "Please provide the document.", filenames)
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertEqual(finding.evidence[0].attachment_name, filename)
                        self.assertEqual(finding.evidence[0].facts[-1], f"Attachment: {filename}")
                        self.assertNotIn(invoice, " ".join(finding.evidence[0].facts))
        # A supplied agreement remains evidence, but its accompanying invoice is
        # not misrepresented as the proof attachment.
        finding = self.finding(Category.MAINTENANCE, "Maintenance",
                               "The signed maintenance agreement is enclosed.", ["maintenance-invoice.pdf"])
        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
        self.assertIsNone(finding.evidence[0].attachment_name)

    def test_completed_document_possession_and_headings_require_complete_objects(self):
        for category, document in [
            (Category.CONTRACT_TERMS, "signed contract"),
            (Category.MAINTENANCE, "signed maintenance agreement"),
        ]:
            for frame in ("We received the {}.", "We have obtained the {}.",
                          "We hold the {}.", "{}.", "{} terms.",
                          "We obtained a {} for service starting next month."):
                with self.subTest(category=category, frame=frame):
                    self.assertEqual(self.finding(category, "Agreement", frame.format(document)).status,
                                     FindingStatus.DOCUMENTED_RECENT)
            for suffix in ("invoice", "billing statement", "terms invoice"):
                for frame in ("We received the {}.", "We have obtained the {}.",
                              "We hold the {}.", "{}."):
                    with self.subTest(category=category, suffix=suffix, frame=frame):
                        self.assert_lead_without_proof_attachment(self.finding(
                            category, "Agreement", frame.format(f"{document} {suffix}")))

    def test_active_and_inverted_invoice_objects_are_not_supplied_documents(self):
        cases = [
            (Category.INSURANCE, "Insurance", "insurance policy"),
            (Category.INSURANCE, "COI", "COI"),
            (Category.INSURANCE, "Insurance", "certificate of insurance"),
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement"),
        ]
        for category, subject, document in cases:
            for suffix in ("invoice", "billing statement"):
                filename = f"{document}-{suffix}.pdf".replace(" ", "-")
                for frame in ("We supplied the {}.", "We have provided the {}.",
                              "Attached is the {}.", "Enclosed is the {}."):
                    statement = frame.format(f"{document} {suffix}")
                    wrapped = frame.format(f"{document}\n{suffix}")
                    layouts = [
                        (statement, None),
                        (wrapped, "text/plain"),
                        (wrapped.replace("\n", "<br>"), "text/html"),
                        ("<p>" + wrapped.replace("\n", "</p><p>") + "</p>", "text/html"),
                    ]
                    for body, mime_type in layouts:
                        for filenames in ((), (filename,)):
                            with self.subTest(category=category, body=body, filenames=filenames):
                                self.assert_lead_without_proof_attachment(self.finding(
                                    category, subject, body, filenames, mime_type=mime_type,
                                    pdf_content=f"The {document} is enclosed.",
                                ))

    def test_supply_object_boundary_is_not_an_invoice_suffix_blacklist(self):
        for category, subject, document in [
            (Category.INSURANCE, "Insurance", "insurance policy"),
            (Category.INSURANCE, "COI", "COI"),
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement"),
        ]:
            for suffix in ("request", "quote", "summary", "payment notice"):
                for frame in ("We supplied the {}.", "Attached is the {}.",
                              "The {} is enclosed."):
                    body = frame.format(f"{document} {suffix}")
                    with self.subTest(category=category, body=body):
                        self.assert_lead_without_proof_attachment(self.finding(category, subject, body))
        # Ambiguous noun coordination is not a boundary introducing an
        # independent supply statement; a plural invoice head can apply to both.
        for body in [
            "We supplied the insurance policy and COI invoices.",
            "Attached is the COI and insurance policy billing statement.",
        ]:
            self.assert_lead_without_proof_attachment(self.finding(Category.INSURANCE, "Insurance", body))

    def test_complete_supply_objects_remain_proof_in_plain_and_html_layouts(self):
        for category, subject, document in [
            (Category.INSURANCE, "Insurance", "insurance policy"),
            (Category.INSURANCE, "COI", "COI"),
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement"),
        ]:
            filename = f"{document}-invoice.pdf".replace(" ", "-")
            for frame in ("We supplied the\n{}.", "We have provided the\n{}.",
                          "Attached is the\n{}.", "Enclosed is the\n{}.",
                          "The {}\nis enclosed for review today."):
                wrapped = frame.format(document)
                for body, mime_type in [
                    (wrapped, "text/plain"),
                    ("<div>" + wrapped.replace("\n", "</div><div>") + "</div>", "text/html"),
                ]:
                    for filenames in ((), (filename,)):
                        with self.subTest(category=category, body=body, filenames=filenames):
                            finding = self.finding(category, subject, body, filenames, mime_type=mime_type)
                            self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                            self.assertIsNone(finding.evidence[0].attachment_name)
                            self.assertTrue(finding.evidence[0].facts[0].endswith("evidence"))

    def test_independently_supplied_documents_survive_invoice_mentions(self):
        for category, subject, document in [
            (Category.INSURANCE, "Insurance", "insurance policy"),
            (Category.INSURANCE, "COI", "COI"),
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement"),
        ]:
            filename = f"{document}-invoice.pdf".replace(" ", "-")
            for statement in [
                f"We supplied the {document}. We supplied the {document} invoice.",
                f"Attached is the {document} billing statement. Attached is the {document}.",
                f"We supplied the {document}, and the {document} invoice is attached.",
                f"We supplied the {document} invoice, and we supplied the {document}.",
                f"Attached is the {document}, and we supplied the invoice.",
                f"We supplied the {document} invoice, and attached is the {document}.",
                f"The invoice is attached, and we supplied the {document}.",
                f"We supplied the invoice, and the {document} is enclosed.",
            ]:
                for body, mime_type in [
                    (statement, "text/plain"),
                    ("<p>" + statement.replace(". ", ".</p><p>") + "</p>", "text/html"),
                ]:
                    for filenames in ((), (filename,)):
                        with self.subTest(category=category, body=body, filenames=filenames):
                            finding = self.finding(category, subject, body, filenames, mime_type=mime_type)
                            self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                            self.assertIsNone(finding.evidence[0].attachment_name)

    def test_true_proof_attachments_override_invoice_only_supply_objects(self):
        for category, subject, document, proof in [
            (Category.INSURANCE, "Insurance", "insurance policy", "policy.pdf"),
            (Category.INSURANCE, "COI", "COI", "COI.docx"),
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract", "agreement.pdf"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement", "maintenance.doc"),
        ]:
            for suffix in ("invoice", "billing statement"):
                excluded = f"{document}-{suffix}.pdf".replace(" ", "-")
                for frame in ("We supplied the {}.", "Attached is the {}."):
                    body = frame.format(f"{document} {suffix}")
                    with self.subTest(category=category, body=body):
                        finding = self.finding(category, subject, body, [excluded, proof])
                        self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                        self.assertEqual(finding.evidence[0].attachment_name, proof)
                        self.assertEqual(finding.evidence[0].facts[-1], f"Attachment: {proof}")
                        self.assertNotIn(excluded, " ".join(finding.evidence[0].facts))

    def test_explicit_coi_and_insurance_policy_supply_without_attachments(self):
        for subject, document in [("COI", "COI"), ("Insurance", "insurance policy")]:
            for body in [f"The {document} is enclosed.", f"The {document} is attached.",
                         f"We supplied the {document}.", f"Attached is the {document}."]:
                with self.subTest(subject=subject, body=body):
                    finding = self.finding(Category.INSURANCE, subject, body)
                    self.assertEqual(finding.status, FindingStatus.DOCUMENTED_RECENT)
                    self.assertIsNone(finding.evidence[0].attachment_name)
                    self.assertEqual(finding.evidence[0].facts, ["Insurance or COI evidence"])
            for body in [f"The invoice for the {document} is attached.",
                         f"We plan to obtain the {document} next month.",
                         f"If the {document} is enclosed, please forward it.",
                         f"The {document} is not enclosed."]:
                with self.subTest(subject=subject, body=body):
                    self.assert_lead_without_proof_attachment(self.finding(Category.INSURANCE, subject, body))

    def test_image_filenames_do_not_establish_documentary_proof(self):
        for filename in ["COI.png", "policy.jpg", "certificate.webp"]:
            with self.subTest(filename=filename):
                self.assert_lead_without_proof_attachment(
                    self.finding(Category.INSURANCE, "Insurance", "Please provide the document.", [filename]))

    def test_supply_predicate_belongs_to_category_document_not_invoice(self):
        cases = [
            (Category.INSURANCE, "Certificate of insurance", "The invoice for the insurance policy is attached."),
            (Category.INSURANCE, "Certificate of insurance", "Certificate of insurance\nThe invoice is attached."),
            (Category.MAINTENANCE, "Maintenance", "We plan to start recurring maintenance next month, and the invoice is attached."),
            (Category.CONTRACT_TERMS, "Signed contract", "The invoice for the signed agreement is attached."),
        ]
        for category, subject, template in cases:
            for body, mime_type in [
                (template, "text/plain"),
                ("<p>" + template.replace("\n", "</p><p>") + "</p>", "text/html"),
                (template.replace("\n", "<br>"), "text/html"),
            ]:
                with self.subTest(category=category, body=body):
                    self.assert_lead_without_proof_attachment(
                        self.finding(category, subject, body, mime_type=mime_type))

    def test_planned_signed_document_acquisition_is_not_completed_evidence(self):
        for category, subject, document, filename in [
            (Category.CONTRACT_TERMS, "Signed contract", "signed contract", "agreement.pdf"),
            (Category.MAINTENANCE, "Maintenance", "signed maintenance agreement", "maintenance.pdf"),
        ]:
            for verb in ["plan to obtain", "intend to obtain", "will obtain"]:
                template = f"We {verb} a {document}\nnext month."
                for body, mime_type in [
                    (template, "text/plain"),
                    ("<div>" + template.replace("\n", "</div><div>") + "</div>", "text/html"),
                ]:
                    with self.subTest(category=category, body=body):
                        self.assert_lead_without_proof_attachment(
                            self.finding(category, subject, body, mime_type=mime_type))
                        self.assertEqual(self.finding(category, subject, body, [filename], mime_type=mime_type).status,
                                         FindingStatus.DOCUMENTED_RECENT)
            for body in [
                f"The {document} is enclosed for service starting next month.",
                f"The {document}s are enclosed for service starting next month.",
                f"We obtained a {document} for service starting next month.",
                f"We plan to start service next month, and the {document} is enclosed.",
                f"The {document} covers service starting next month.",
            ]:
                self.assertEqual(self.finding(category, subject, body).status, FindingStatus.DOCUMENTED_RECENT)

    def test_other_category_supplied_proof_does_not_rescue_a_request(self):
        cases = [
            (Category.CONTRACT_TERMS, "Signed contract", "Please provide a signed contract. The insurance certificate was issued."),
            (Category.INSURANCE, "Certificate of insurance", "Please provide a certificate of insurance. The signed contract is enclosed."),
            (Category.MAINTENANCE, "Preventive maintenance plan", "Please send a maintenance plan. The insurance certificate was issued."),
        ]
        for category, subject, body in cases:
            with self.subTest(category=category):
                self.assert_lead_without_proof_attachment(self.finding(category, subject, body))


if __name__ == "__main__":
    unittest.main()
