import unittest

from review_processes.vendor_review.detect import detect_electricity_transition


class DetectionTests(unittest.TestCase):
  def test_executed_electricity_agreement_proposes_provider_transition(self):
    vendors = [{
        "id": "rec_old",
        "fields": {
            "Vendor": "Old Power LLC",
            "Stores": ["RoundRock"],
            "Notes": "Electric Bill",
            "Website": "https://old.example",
            "Contact Email": "old@example.com",
            "Contact Email 2": "old@example.com",
            "Account #": "OLD-123",
        },
    }]
    message = {
        "id": "gmail_1",
        "internalDate": "1",
        "payload": {"headers": [
            {"name": "Subject", "value": "Electric Enrollment"},
            {"name": "From", "value": "customer-support@gridmatic.com"},
        ]},
    }
    text = """
    COMMERCIAL ELECTRICITY SUPPLY AGREEMENT
    Company: Gridmatic Rosa LLC, dba Gridmatic Retail
    Service Address: 2000 N Mays St Ste 108 Round Rock TX
    Price: $ .06623/kWh
    Term: 60 month(s)
    Email: retail-ops@gridmatic.com
    Toll Free Number: 877-744-7718
    Website: www.gridmaticretail.com
    ESIID 10443720007128041
    Meter Reading On or After 8/2026
    """
    proposal = detect_electricity_transition(vendors, message, text, {"Executed.pdf": b"pdf"})
    self.assertIsNotNone(proposal)
    self.assertEqual(proposal.store, "RoundRock")
    changes = {change.field_name: change.after for change in proposal.changes}
    self.assertTrue(changes["Vendor"].lower().startswith("gridmatic rosa"))
    self.assertEqual(changes["Contact Email"], "retail-ops@gridmatic.com")
    self.assertEqual(changes["Contact Email 2"], "customer-support@gridmatic.com")
    self.assertEqual(changes["Account #"], "")
    self.assertEqual(changes["Website"], "https://www.gridmaticretail.com")
    self.assertEqual(proposal.attachments[0].filename, "Executed.pdf")

  def test_invoice_without_agreement_does_not_rename_vendor(self):
    self.assertIsNone(detect_electricity_transition([], {"id": "x"}, "Invoice from New Power", {}))

  def test_flattened_pdf_terms_are_recovered(self):
    vendors = [{"id": "rec", "fields": {"Vendor": "Old", "Stores": ["RoundRock"], "Notes": "Electric Bill"}}]
    message = {"id": "m", "payload": {"headers": []}}
    text = """COMMERCIAL ELECTRICITY SUPPLY AGREEMENT
    Gridmatic Rosa LLC, dba Gridmatic Retail
    Email: retail-ops@gridmatic.com Website: www.gridmaticretail.com
    Phone: 877-744-7718 cleo@example.com .06623 60 Cleo McAllister
    10443720007128041 2000 N Mays St Ste 108 Round Rock, TX, 78664 8/2026
    """
    proposal = detect_electricity_transition(vendors, message, text, {})
    notes = next(change.after for change in proposal.changes if change.field_name == "Notes")
    self.assertIn("$0.06623/kWh", notes)
    self.assertIn("60 months", notes)
    self.assertIn("8/2026", notes)


if __name__ == "__main__":
    unittest.main()
