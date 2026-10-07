import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from review_processes.vendor_review.detect import detect_electricity_transition
from review_processes.vendor_review.models import Proposal
from review_processes.vendor_review.service import VendorReviewService
from review_processes.vendor_review.store import ProposalStore


TEXT = """COMMERCIAL ELECTRICITY SUPPLY AGREEMENT
Gridmatic Rosa LLC, dba Gridmatic Retail
Round Rock TX
Email: retail-ops@gridmatic.com
Website: www.gridmaticretail.com
Term: 60 months
"""
MESSAGE = {"id": "contract-1", "payload": {"headers": []}}


class Airtable:
    def __init__(self):
        self.vendor = {"id": "rec", "fields": {"Vendor": "Old Power", "Stores": ["RoundRock"],
                       "Notes": "Electric Bill", "Account #": "OLD", "Payment Method": "Check"}}
        self.updates = []

    def record(self, record_id):
        return copy.deepcopy(self.vendor)

    def update(self, record_id, fields):
        self.updates.append(dict(fields))
        self.vendor["fields"].update(fields)


class ReplayTests(unittest.TestCase):
    def test_apply_then_same_day_and_next_day_scan_do_not_clear_new_account(self):
        with tempfile.TemporaryDirectory() as directory:
            airtable = Airtable()
            store = ProposalStore(Path(directory))
            proposal = detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {})
            self.assertEqual(proposal.source_fields, {"Website": None, "Account #": "OLD", "Payment Method": "Check"})
            self.assertEqual(Proposal.from_dict(proposal.to_dict()).source_fields, proposal.source_fields)
            store.upsert([proposal])
            VendorReviewService(airtable, object(), store).apply(store.get(proposal.id))
            airtable.vendor["fields"]["Account #"] = "NEW-ACCOUNT"
            for display in ("Gridmatic Rosa LLC, dba Gridmatic Retail", "Gridmatic Retail",
                            "GRIDMATIC RETAIL", "Gridmatic Rosa LLC"):
                airtable.vendor["fields"]["Vendor"] = display
                for day in (date(2026, 8, 26), date(2026, 8, 27)):
                    with self.subTest(display=display, day=day), patch("review_processes.vendor_review.detect.date") as clock:
                        clock.today.return_value = day
                        replay = detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {})
                    self.assertIsNone(replay)
                    store.upsert([] if replay is None else [replay])
                    self.assertFalse(any(p.status == "pending" for p in store.load()))
            self.assertEqual(airtable.vendor["fields"]["Account #"], "NEW-ACCOUNT")
            self.assertEqual(len(airtable.updates), 1)
            # A different provider's contract is still a human-review proposal.
            fresh = detect_electricity_transition([airtable.vendor], {**MESSAGE, "id": "contract-2"},
                TEXT.replace("Gridmatic Rosa LLC, dba Gridmatic Retail", "Other Power LLC, dba Other Retail"), {})
            self.assertIsNotNone(fresh)
            store.upsert([fresh])
            self.assertEqual(store.get(fresh.id).status, "pending")

    def test_legacy_approved_payload_applies_unchanged_through_service(self):
        with tempfile.TemporaryDirectory() as directory:
            airtable = Airtable()
            store = ProposalStore(Path(directory))
            item = detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {})
            item.status = "approved"
            store.save([item])
            approved = store.get(item.id).to_dict()
            service = VendorReviewService(airtable, object(), store)
            # Dry-run neither writes nor consumes the legacy approval.
            service.apply(store.get(item.id), dry_run=True)
            self.assertEqual(airtable.updates, [])
            self.assertEqual(store.get(item.id).to_dict(), approved)
            service.apply(store.get(item.id))
            approved["status"] = "applied"
            self.assertEqual(store.get(item.id).to_dict(), approved)
            self.assertEqual(len(airtable.updates), 1)
            with self.assertRaisesRegex(RuntimeError, "applied"):
                service.apply(store.get(item.id))
            self.assertEqual(len(airtable.updates), 1)

    def test_dba_alias_replay_is_blocked_without_local_proposal_history(self):
        airtable = Airtable()
        airtable.vendor["fields"].update({"Vendor": "Gridmatic Retail", "Account #": "NEW-ACCOUNT"})
        self.assertIsNone(detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {}))
        self.assertEqual(airtable.vendor["fields"]["Account #"], "NEW-ACCOUNT")
        self.assertEqual(airtable.updates, [])

    def test_same_evidence_has_stable_identity_even_when_source_fields_change(self):
        airtable = Airtable()
        original = detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {})
        airtable.vendor["fields"].update({"Website": "https://manually-edited.example", "Account #": "DIFFERENT"})
        regenerated = detect_electricity_transition([airtable.vendor], MESSAGE, TEXT, {})
        self.assertEqual(original.fingerprint, regenerated.fingerprint)
        fresh = detect_electricity_transition([airtable.vendor], {**MESSAGE, "id": "different-contract"}, TEXT, {})
        self.assertNotEqual(original.fingerprint, fresh.fingerprint)
