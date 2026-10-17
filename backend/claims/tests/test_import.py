"""Tests del import del Excel (import_claims): Claim + ClaimOutcome por separado.

    docker compose exec backend python manage.py test claims.tests.test_import
"""

import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

import openpyxl
from django.core.management import call_command
from django.test import TestCase

from claims.models import Claim, ClaimOutcome

HEADERS = ["Tracking Number", "Move-Out Date", "Amount of Claim", "Max Benefit", "Approved Benefit Amount",
           "Status", "Posted Date", "Hold Reason"]
ROWS = [
    ["1", "03/01/22", "$1.00", "$2,500.00", "$2,500.00", "Posted", "03/14/22", ""],
    ["2", "04/10/22", "$900.00", "$1,500.00", "$0.00", "Declined", "", "No lease provided"],
    ["3", "05/01/22", "$300.00", "$1,000.00", "per PM, tenant paid directly", "Posted", "", ""],
]


class ImportClaimsTests(TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.path = tmp / "claims.xlsx"
        wb = openpyxl.Workbook()
        wb.active.append(HEADERS)
        for row in ROWS:
            wb.active.append(row)
        wb.save(self.path)

    def run_import(self, *args):
        out = StringIO()
        call_command("import_claims", str(self.path), "--test-size", "1", *args, stdout=out)
        return out.getvalue()

    def test_outcome_is_stored_apart_from_claim(self):
        output = self.run_import()

        claim = Claim.objects.get(pk="1")
        self.assertEqual((claim.claim_amount, claim.max_benefit), (Decimal("1.00"), Decimal("2500.00")))
        outcome = ClaimOutcome.objects.get(claim=claim)
        self.assertEqual((outcome.approved_benefit, outcome.status), (Decimal("2500.00"), "Posted"))
        self.assertEqual(outcome.raw_outcome["Approved Benefit Amount"], "$2,500.00")
        self.assertIn("Resultados (ClaimOutcome): 3", output)

    def test_result_columns_never_reach_claim(self):
        self.run_import()
        for raw_row in Claim.objects.values_list("raw_row", flat=True):
            self.assertFalse({"Approved Benefit Amount", "Status", "Posted Date", "Hold Reason"} & raw_row.keys())

    def test_declined_and_unreadable_amounts(self):
        self.run_import()
        declined = ClaimOutcome.objects.get(claim_id="2")
        self.assertEqual((declined.approved_benefit, declined.status, declined.hold_reason),
                         (Decimal("0.00"), "Declined", "No lease provided"))
        unreadable = ClaimOutcome.objects.get(claim_id="3")
        self.assertIsNone(unreadable.approved_benefit)  # texto en lugar de monto: el original queda en raw_outcome
        self.assertEqual(unreadable.raw_outcome["Approved Benefit Amount"], "per PM, tenant paid directly")

    def test_running_twice_does_not_duplicate(self):
        self.run_import()
        self.run_import()
        self.assertEqual((Claim.objects.count(), ClaimOutcome.objects.count()), (3, 3))

    def test_test_set_only_uses_claims_with_ground_truth(self):
        self.run_import()
        chosen = Claim.objects.get(is_test_set=True)
        self.assertIn(chosen.pk, {"1", "2"})  # el 3 no tiene monto aprobado legible

    def test_dry_run_saves_nothing(self):
        self.run_import("--dry-run")
        self.assertEqual((Claim.objects.count(), ClaimOutcome.objects.count()), (0, 0))
