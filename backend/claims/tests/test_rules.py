"""Tests del motor de reglas. Código puro: sin LLM.

    docker compose exec backend python manage.py test claims.tests.test_rules
"""

from datetime import date
from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from claims.llm.schemas import ClaimExtraction as ClaimExtractionSchema
from claims.models import Claim, ClaimDocument, ClaimExtraction, LeaseExtraction, Recommendation
from claims.pipeline.rules import (
    RULES_VERSION,
    Charge,
    ClaimData,
    Extraction,
    FeeTerm,
    LeaseTerms,
    Payment,
    apply_fifo,
    build_recommendation,
    evaluate,
    extraction_from_json,
    lease_from_json,
    usd,
)


def claim(max_benefit="2500.00", claim_amount=None, monthly_rent=None):
    return ClaimData(
        max_benefit=D(max_benefit) if max_benefit is not None else None,
        claim_amount=D(claim_amount) if claim_amount is not None else None,
        monthly_rent=D(monthly_rent) if monthly_rent is not None else None,
    )


def charge(amount, category="cleaning", eligible=True, day=None, **kw):
    return Charge(description=category, amount=D(amount), category=category, eligible=eligible,
                  date=date(2022, 1, day) if day else None, **kw)


def run(charges=(), payments=(), lease=None, claim_data=None, doc_texts=None, **ext):
    extraction = Extraction(charges=list(charges), payments=list(payments), confidence=ext.pop("confidence", "high"), **ext)
    return evaluate(extraction, lease, claim_data or claim(), doc_texts)


class FifoTests(SimpleTestCase):
    def test_partial_payments_go_to_oldest_charges_first(self):
        rent = charge("1000.00", "unpaid_rent", day=1)
        cleaning = charge("300.00", day=15)
        undated = charge("50.00", "repairs")

        result = apply_fifo([undated, cleaning, rent], [Payment("payment", D("1100.00"), date(2022, 2, 1))])

        self.assertEqual([(paid, bal) for _, paid, bal in result],
                         [(D("0.00"), D("50.00")), (D("100.00"), D("200.00")), (D("1000.00"), D("0.00"))])

    def test_undated_charges_go_last(self):
        result = apply_fifo([charge("100.00"), charge("100.00", day=5)], [Payment("credit", D("100.00"))])
        self.assertEqual([bal for _, _, bal in result], [D("100.00"), D("0.00")])

    def test_payment_credit_and_concession_count_other_types_do_not(self):
        payments = [Payment("payment", D("10")), Payment("credit", D("20")), Payment("concession", D("30")),
                    Payment("deposit", D("500"))]
        result = apply_fifo([charge("100.00", day=1)], payments)
        self.assertEqual(result[0][2], D("40.00"))

    def test_payments_can_cover_ineligible_charges_first(self):
        result = run(
            charges=[charge("100.00", "late_fee", eligible=False, day=1), charge("200.00", day=2)],
            payments=[Payment("payment", D("100.00"))],
        )
        self.assertEqual(result.eligible_owed, D("200.00"))
        self.assertEqual(result.excluded_total, D("0.00"))

    def test_eligible_owed_is_sum_of_remaining_eligible_balances(self):
        result = run(
            charges=[charge("500.00", day=1), charge("300.00", "late_fee", eligible=False, day=2), charge("400.00", day=3)],
            payments=[Payment("payment", D("200.00"))],
        )
        self.assertEqual(result.eligible_owed, D("700.00"))  # 300 + 400
        self.assertEqual(result.excluded_total, D("300.00"))

    def test_deposit_held_is_subtracted_after_fifo(self):
        result = run(charges=[charge("1000.00", day=1)], payments=[Payment("payment", D("200.00"))],
                     deposit_held=D("300.00"))
        self.assertEqual(result.eligible_owed, D("800.00"))
        self.assertEqual(result.recommended_amount, D("500.00"))


class OverrideTests(SimpleTestCase):
    def eligibility(self, category, llm_eligible):
        r = run(charges=[charge("100.00", category, eligible=llm_eligible)]).charges[0]
        return r.eligible, r.eligibility_source

    def test_override_false_wins_over_llm(self):
        self.assertEqual(self.eligibility("program_fee", True), (False, "override"))
        self.assertEqual(self.eligibility("normal_wear_and_tear", True), (False, "override"))

    def test_none_respects_llm(self):
        for category in ("late_fee", "legal_fee", "admin_fee", "convenience_fee"):
            self.assertEqual(self.eligibility(category, True), (True, "llm"), category)
            self.assertEqual(self.eligibility(category, False), (False, "llm"), category)

    def test_category_not_in_table_respects_llm(self):
        self.assertEqual(self.eligibility("cleaning", True), (True, "llm"))

    def test_no_llm_judgment_excludes(self):
        self.assertEqual(self.eligibility("cleaning", None), (False, "default"))

    def test_reasons_are_in_english(self):
        override = run(charges=[charge("100.00", "program_fee", eligible=True)]).charges[0]
        default = run(charges=[charge("100.00", "cleaning", eligible=None)]).charges[0]
        self.assertEqual(override.reason, f"Rule {RULES_VERSION}: program_fee is never eligible")
        self.assertEqual(default.reason, "The LLM did not judge eligibility; excluded by default")

    def test_usd_puts_the_sign_before_the_dollar(self):
        self.assertEqual([usd(D("-32479.73")), usd(D("2500")), usd(None)], ["-$32,479.73", "$2,500.00", "—"])


class AmountTests(SimpleTestCase):
    def test_cap_applied(self):
        result = run(charges=[charge("3000.00")])
        self.assertEqual((result.recommended_amount, result.cap_applied), (D("2500.00"), True))

    def test_below_cap(self):
        result = run(charges=[charge("2000.00")])
        self.assertEqual((result.recommended_amount, result.cap_applied), (D("2000.00"), False))

    def test_never_negative(self):
        result = run(charges=[charge("100.00")], deposit_held=D("500.00"))
        self.assertEqual(result.recommended_amount, D("0.00"))

    def test_overpaid_is_not_negative(self):
        result = run(charges=[charge("100.00")], payments=[Payment("payment", D("400.00"))])
        self.assertEqual(result.recommended_amount, D("0.00"))

    def test_missing_max_benefit_routes_to_review(self):
        result = run(charges=[charge("3000.00")], claim_data=claim(max_benefit=None))
        self.assertIn("missing_max_benefit", result.flags)
        self.assertEqual((result.route, result.recommended_amount), ("review", D("3000.00")))


class FlagTests(SimpleTestCase):
    def flags(self, **kw):
        return run(**kw).flags

    def test_totals_mismatch(self):
        charges = [charge("150.00"), charge("50.00")]
        payments = [Payment("payment", D("50.00"))]
        self.assertIn("totals_mismatch", self.flags(charges=charges, payments=payments, printed_total=D("100.00")))
        self.assertNotIn("totals_mismatch", self.flags(charges=charges, payments=payments, printed_total=D("150.90")))
        self.assertNotIn("totals_mismatch", self.flags(charges=charges, payments=payments))  # sin total impreso

    def test_claim_amount_mismatch(self):
        charges = [charge("1300.00")]
        self.assertIn("claim_amount_mismatch", self.flags(charges=charges, claim_data=claim(claim_amount="1.00")))
        self.assertIn("claim_amount_mismatch", self.flags(charges=charges, claim_data=claim(claim_amount="1000.00")))
        self.assertNotIn("claim_amount_mismatch", self.flags(charges=charges, claim_data=claim(claim_amount="1100.00")))
        self.assertNotIn("claim_amount_mismatch", self.flags(charges=charges, claim_data=claim(claim_amount=None)))

    def test_claim_amount_compares_against_printed_total_when_present(self):
        flags = self.flags(charges=[charge("5000.00")], printed_total=D("1000.00"), claim_data=claim(claim_amount="1000.00"))
        self.assertNotIn("claim_amount_mismatch", flags)

    def test_fee_exceeds_fixed(self):
        lease = LeaseTerms(fees={"late_fee": [FeeTerm("fixed", D("50.00"))]})
        self.assertIn("fee_exceeds_lease_terms", self.flags(charges=[charge("75.00", "late_fee")], lease=lease))
        self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("50.00", "late_fee")], lease=lease))

    def test_fee_exceeds_percent_of_rent(self):
        lease = LeaseTerms(fees={"late_fee": [FeeTerm("percent_of_rent", D("5"))]})  # 5% de 1000 = 50
        rent = claim(monthly_rent="1000.00")
        self.assertIn("fee_exceeds_lease_terms", self.flags(charges=[charge("60.00", "late_fee")], lease=lease, claim_data=rent))
        self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("50.00", "late_fee")], lease=lease, claim_data=rent))

    def test_fee_with_several_terms_uses_the_largest(self):
        lease = LeaseTerms(fees={"late_fee": [FeeTerm("fixed", D("50.00")), FeeTerm("percent_of_rent", D("10"))]})
        rent = claim(monthly_rent="1000.00")  # límites: 50 y 100
        self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("100.00", "late_fee")], lease=lease, claim_data=rent))
        self.assertIn("fee_exceeds_lease_terms", self.flags(charges=[charge("101.00", "late_fee")], lease=lease, claim_data=rent))

    def test_per_unit_and_per_day_cannot_be_checked(self):
        # Prompt B no extrae cantidades (cerraduras, días): no se puede afirmar que el cargo supere el término
        for kind in ("per_unit", "per_day"):
            lease = LeaseTerms(fees={"rekey": [FeeTerm(kind, D("10.00"))]})
            self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("999.00", "rekey")], lease=lease))

    def test_fixed_plus_per_day_cannot_be_checked(self):
        lease = LeaseTerms(fees={"late_fee": [FeeTerm("fixed", D("50.00")), FeeTerm("per_day", D("10.00"))]})
        self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("120.00", "late_fee")], lease=lease))

    def test_fee_without_lease_is_not_flagged(self):
        self.assertNotIn("fee_exceeds_lease_terms", self.flags(charges=[charge("999.00", "late_fee")]))

    def test_coverage_mismatch(self):
        self.assertIn("coverage_mismatch", self.flags(lease=LeaseTerms(coverage_amount=D("2000.00"))))
        self.assertNotIn("coverage_mismatch", self.flags(lease=LeaseTerms(coverage_amount=D("2500.00"))))
        self.assertNotIn("coverage_mismatch", self.flags(lease=LeaseTerms()))

    def test_amount_not_in_text(self):
        text = "TENANT LEDGER " + "x" * 100 + " Carpet replacement $1,234.56  Paint 300.00"
        texts = {"ledger.pdf": text}
        found = [charge("1234.56", source_document="ledger.pdf"), charge("300.00", source_document="ledger.pdf")]
        invented = [charge("999.99", source_document="ledger.pdf")]
        self.assertNotIn("amount_not_in_text", self.flags(charges=found, doc_texts=texts))
        self.assertIn("amount_not_in_text", self.flags(charges=invented, doc_texts=texts))

    def test_amount_not_in_text_skips_documents_without_text(self):
        flags = self.flags(charges=[charge("999.99", source_document="scan.pdf")], doc_texts={"scan.pdf": "  page 1 "})
        self.assertNotIn("amount_not_in_text", flags)

    def test_rent_mismatch(self):
        rent = claim(monthly_rent="1450.00")
        self.assertIn("rent_mismatch", self.flags(charges=[charge("1500.00", "unpaid_rent")], claim_data=rent))
        self.assertNotIn("rent_mismatch", self.flags(charges=[charge("1450.00", "unpaid_rent")], claim_data=rent))
        self.assertNotIn("rent_mismatch", self.flags(charges=[charge("700.00", "unpaid_rent", prorated=True)], claim_data=rent))

    def test_llm_flags_are_kept(self):
        self.assertIn("photos_only", self.flags(flags=["photos_only"]))


class RouteTests(SimpleTestCase):
    def route(self, confidence, **kw):
        return run(charges=[charge("100.00")], confidence=confidence, **kw).route

    def test_auto_with_high_confidence_and_no_critical_flags(self):
        self.assertEqual(self.route("high"), "auto")
        self.assertEqual(self.route("high", claim_data=claim(claim_amount="1.00")), "auto")  # flag no crítico

    def test_critical_flag_goes_to_review(self):
        for flag in ("illegible_document", "photos_only", "missing_documentation"):
            self.assertEqual(self.route("high", flags=[flag]), "review", flag)

    def test_totals_mismatch_with_low_or_medium_confidence_retries(self):
        self.assertEqual(self.route("medium", printed_total=D("500.00")), "retry_stronger_model")
        self.assertEqual(self.route("low", printed_total=D("500.00")), "retry_stronger_model")
        self.assertEqual(self.route("high", printed_total=D("500.00")), "review")

    def test_medium_without_flags_goes_to_review(self):
        self.assertEqual(self.route("medium"), "review")


class Claim1Tests(SimpleTestCase):
    """Caso real del claim 1: ledger de $9,289.19 sin pagos, max_benefit $2,500, claim_amount $1.00."""

    def test_recommends_cap_with_claim_amount_mismatch(self):
        result = run(
            charges=[
                charge("7063.23", "cleaning", day=1),
                charge("1500.00", "late_fee", eligible=False, day=2),
                charge("725.96", "admin_fee", eligible=False, day=3),
            ],
            claim_data=claim(max_benefit="2500.00", claim_amount="1.00"),
        )

        self.assertEqual(result.charges_total, D("9289.19"))
        self.assertEqual(result.recommended_amount, D("2500.00"))
        self.assertTrue(result.cap_applied)
        self.assertIn("claim_amount_mismatch", result.flags)
        self.assertEqual(
            result.explanation,
            "Documented charges: $9,289.19. Eligible after payments: $7,063.23. Applied the cap of $2,500.00. "
            "Excluded: $2,225.96 (late fees, admin fees). Recommended: $2,500.00. "
            "⚠ The claimed amount ($1.00) doesn't match the documented total ($9,289.19).",
        )


class AdapterTests(SimpleTestCase):
    def line_item(self, **kw):
        item = {"date": "2022-03-01", "description": "Carpet", "amount": 1234.5, "category": "damages",
                "eligible": True, "eligibility_basis": "lease", "lease_reference": "B.6.E",
                "reason": "beyond wear", "source_document": "ledger.pdf"}
        return {**item, **kw}

    def test_extraction_from_json(self):
        raw = {
            "line_items": [self.line_item(), self.line_item(description="Prorated rent 3/1-3/15", category="unpaid_rent")],
            "payments_and_credits": [
                {"date": "2022-02-01", "description": "Payment", "amount": 100, "type": "payment", "source_document": "l.pdf"},
                {"date": None, "description": "Deposit on hand", "amount": 300, "type": "deposit_held", "source_document": "l.pdf"},
                {"date": None, "description": "Deposit 2", "amount": 200, "type": "deposit_held", "source_document": "l.pdf"},
            ],
            "printed_total": None, "findings": [], "flags": ["photos_only"], "confidence": "high",
        }
        ClaimExtractionSchema.model_validate(raw)  # el JSON de prueba es válido para el esquema del apéndice

        ext = extraction_from_json(raw)

        first = ext.charges[0]
        self.assertEqual((first.amount, first.date, first.category, first.lease_reference, first.prorated),
                         (D("1234.50"), date(2022, 3, 1), "damages", "B.6.E", False))
        self.assertTrue(ext.charges[1].prorated)
        self.assertEqual(ext.deposit_held, D("500.00"))  # suma de los deposit_held
        self.assertEqual((ext.printed_total, ext.flags, ext.confidence), (None, ["photos_only"], "high"))

    def test_deposit_held_is_not_applied_as_payment(self):
        raw = {"line_items": [self.line_item(amount=1000)], "confidence": "high", "payments_and_credits": [
            {"date": None, "description": "Deposit", "amount": 300, "type": "deposit_held", "source_document": "l.pdf"}]}
        result = evaluate(extraction_from_json(raw), None, claim())
        self.assertEqual((result.eligible_owed, result.recommended_amount), (D("1000.00"), D("700.00")))

    def test_lease_from_json(self):
        lease = lease_from_json({
            "program": {"coverage_amount": 2500.0},
            "fees": [
                {"name": "Late", "category": "late_fee", "amount_type": "fixed", "value": 50, "unit": None},
                {"name": "Daily late", "category": "late_fee", "amount_type": "per_day", "value": 10, "unit": None},
                {"name": "Misc", "category": "other", "amount_type": "fixed", "value": 5, "unit": None},
            ],
        })
        self.assertEqual(lease.coverage_amount, D("2500.00"))
        self.assertEqual(lease.fees["late_fee"], [FeeTerm("fixed", D("50.00")), FeeTerm("per_day", D("10.00"))])
        self.assertNotIn("other", lease.fees)
        self.assertIsNone(lease_from_json(None))
        self.assertIsNone(lease_from_json({"program": None, "fees": []}).coverage_amount)


class RecommendationTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(tracking_id="1", max_benefit=D("2500.00"), claim_amount=D("1.00"))
        ClaimDocument.objects.create(claim=self.claim, file_path="claims/x/ledger.pdf", file_name="ledger.pdf",
                                     file_hash="h", full_text="LEDGER " + "x" * 100 + " Total 9,289.19")
        self.lease = LeaseExtraction.objects.create(claim=self.claim, is_lease=True, prompt_version="lease-v1",
                                                    model_used="m", cache_key="k",
                                                    raw_json={"program": {"coverage_amount": 2500.0}, "fees": []})
        self.extraction = ClaimExtraction.objects.create(
            claim=self.claim, lease_extraction=self.lease, prompt_version="claim-v1", model_used="m", cache_key="k",
            raw_json={"line_items": [{"date": None, "description": "Ledger balance", "amount": 9289.19,
                                      "category": "cleaning", "eligible": True, "eligibility_basis": "lease",
                                      "lease_reference": None, "reason": "r", "source_document": "ledger.pdf"}],
                      "payments_and_credits": [], "printed_total": None, "findings": [], "flags": [],
                      "confidence": "high"},
        )

    def test_build_recommendation_is_not_saved(self):
        rec = build_recommendation(self.claim, self.extraction, self.lease)

        self.assertIsNone(rec.pk)
        self.assertEqual(Recommendation.objects.count(), 0)
        self.assertEqual((rec.recommended_amount, rec.route, rec.rules_version), (D("2500.00"), "auto", RULES_VERSION))
        self.assertEqual(rec.flags, ["claim_amount_mismatch"])
        self.assertEqual(rec.reasoning[0]["balance"], "9289.19")

    def test_recompute_command_updates_instead_of_duplicating(self):
        out = StringIO()
        call_command("recompute_recommendations", "--all", stdout=out)
        call_command("recompute_recommendations", "--ids", "1", stdout=out)

        rec = Recommendation.objects.get()
        self.assertEqual((rec.recommended_amount, rec.rules_version), (D("2500.00"), RULES_VERSION))
        self.assertIn("1 creadas, 0 actualizadas", out.getvalue())
        self.assertIn("0 creadas, 1 actualizadas", out.getvalue())
