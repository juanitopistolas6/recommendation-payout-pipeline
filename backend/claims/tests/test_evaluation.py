"""Tests de la evaluación. Cada número esperado está calculado a mano en el comentario.

    docker compose exec backend python manage.py test claims.tests.test_evaluation
"""

from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from claims.evaluation.metrics import baseline, bias, claim_rows, evaluate, improvement_pct, mae
from claims.management.commands.evaluate import improvement
from claims.models import Claim, ClaimExtraction, ClaimOutcome, Recommendation

# pred:   100   301    0   700   50
# humano: 100   200    0   800    0
# pred-h:   0  +101    0  -100  +50
# |err|:    0   101    0   100   50
PREDS = [D("100"), D("301"), D("0"), D("700"), D("50")]
HUMANS = [D("100"), D("200"), D("0"), D("800"), D("0")]


class MetricTests(SimpleTestCase):
    def test_mae(self):
        # (0 + 101 + 0 + 100 + 50) / 5 = 251 / 5 = 50.20
        self.assertEqual(mae(PREDS, HUMANS), D("50.20"))

    def test_mae_rounds_to_cents(self):
        # (1 + 1 + 0) / 3 = 0.666... -> 0.67
        self.assertEqual(mae([D("1"), D("1"), D("0")], [D("0"), D("0"), D("0")]), D("0.67"))

    def test_bias(self):
        # (0 + 101 + 0 - 100 + 50) / 5 = 51 / 5 = 10.20 (paga de más en promedio)
        self.assertEqual(bias(PREDS, HUMANS), D("10.20"))

    def test_bias_negative(self):
        # (-100 + -50) / 2 = -75.00
        self.assertEqual(bias([D("100"), D("50")], [D("200"), D("100")]), D("-75.00"))

    def test_empty(self):
        self.assertIsNone(mae([], []))
        self.assertIsNone(bias([], []))

    def test_lengths_must_match(self):
        with self.assertRaises(ValueError):
            mae([D("1")], [D("1"), D("2")])


class ImprovementTests(SimpleTestCase):
    def test_normal(self):
        # (2114.54 - 663.00) / 2114.54 × 100 = 1451.54 / 2114.54 × 100 = 68.645... -> 68.6
        self.assertEqual(improvement_pct(D("2114.54"), D("663.00")), D("68.6"))

    def test_rounds_half_up(self):
        # (200 - 189.9) / 200 × 100 = 5.05 -> 5.1 (no 5.0)
        self.assertEqual(improvement_pct(D("200"), D("189.9")), D("5.1"))

    def test_baseline_without_error_is_none(self):
        self.assertIsNone(improvement_pct(D("0.00"), D("0.00")))
        self.assertIsNone(improvement_pct(D("0.00"), D("50.00")))

    def test_ai_worse_than_baseline_is_negative(self):
        # (100 - 150) / 100 × 100 = -50.0: no se recorta a 0
        self.assertEqual(improvement_pct(D("100.00"), D("150.00")), D("-50.0"))

    def test_missing_values(self):
        self.assertIsNone(improvement_pct(None, D("1")))
        self.assertIsNone(improvement_pct(D("1"), None))

    def test_command_wording(self):
        self.assertEqual(improvement(D("68.6")), "68.6% menos error")
        self.assertEqual(improvement(D("-50.0")), "50.0% más error")
        self.assertEqual(improvement(None), "mejora —")


class BaselineTests(SimpleTestCase):
    def test_baseline(self):
        self.assertEqual(baseline(D("500"), D("300")), D("300"))   # tope
        self.assertEqual(baseline(D("200"), D("300")), D("200"))
        self.assertEqual(baseline(D("200"), None), D("200"))       # sin max_benefit no hay tope
        self.assertEqual(baseline(D("-20"), D("300")), D("0.00"))  # nunca un pago negativo


def make_case(tracking, *, claim_amount="1000.00", max_benefit="800.00", human="700.00", ai="750.00",
              route="auto", rules_version="v1", outcome=True, flags=()):
    claim = Claim.objects.create(
        tracking_id=tracking,
        claim_amount=D(claim_amount) if claim_amount is not None else None,
        max_benefit=D(max_benefit) if max_benefit is not None else None,
    )
    if outcome:
        ClaimOutcome.objects.create(claim=claim, approved_benefit=D(human) if human is not None else None)
    extraction = ClaimExtraction.objects.create(
        claim=claim, prompt_version="claim-v1", model_used="m", cache_key=tracking, confidence="high",
    )
    Recommendation.objects.create(
        claim=claim, extraction=extraction, recommended_amount=D(ai), eligible_owed=D(ai), excluded_total=D("0"),
        route=route, rules_version=rules_version, flags=list(flags),
    )
    return claim


class EvaluationDataTests(TestCase):
    """Conjunto: A, B y G. C, D, E y F no entran.

        claim  tope   humano  IA    regla  IA-humano  |IA-h|  |regla-h|
        A  1000   800    700   750    800      +50        50      100
        B   500  1000      0     0    500        0         0      500
        G  1000   800    900   600    800     -300       300      100

        MAE IA    = (50 + 0 + 300) / 3   = 116.666... -> 116.67
        Bias IA   = (50 + 0 - 300) / 3   = -83.333... -> -83.33
        MAE regla = (100 + 500 + 100) / 3 = 233.333... -> 233.33
        Mejora    = (233.33 - 116.67) / 233.33 × 100 = 49.998... -> 50.0
    """

    def setUp(self):
        make_case("A", flags=["rent_mismatch"])
        make_case("B", claim_amount="500.00", max_benefit="1000.00", human="0.00", ai="0.00", route="review")
        make_case("G", human="900.00", ai="600.00", flags=["claim_amount_mismatch"])
        make_case("C", outcome=False)                    # sin decisión humana
        make_case("D", human=None)                       # approved_benefit nulo
        make_case("E", rules_version="v0")               # otra versión de reglas
        make_case("F", claim_amount=None)                # sin claim_amount: no hay regla simple

    def test_evaluate(self):
        self.assertEqual(evaluate("v1"), {
            "rules_version": "v1",
            "n": 3,
            "ai_mae": D("116.67"),
            "ai_bias": D("-83.33"),
            "baseline_name": "Regla simple",
            "baseline_description": "min(Amount of Claim, Max Benefit)",
            "baseline_mae": D("233.33"),
            "improvement_pct": D("50.0"),
        })

    def test_other_rules_version(self):
        # E sola: |750 - 700| = 50; regla |800 - 700| = 100 -> mejora 50.0
        report = evaluate("v0")
        self.assertEqual((report["n"], report["ai_mae"], report["improvement_pct"]), (1, D("50.00"), D("50.0")))

    def test_claim_rows_signed_difference_sorted_by_abs_error(self):
        rows = claim_rows("v1")

        self.assertEqual([r["tracking_number"] for r in rows], ["G", "A", "B"])  # 300, 50, 0
        self.assertEqual(rows[0], {"tracking_number": "G", "human": D("900.00"), "ai": D("600.00"),
                                   "difference": D("-300.00"), "abs_error": D("300.00"), "route": "auto",
                                   "flags": ["claim_amount_mismatch"]})
        self.assertEqual((rows[1]["difference"], rows[2]["difference"]), (D("50.00"), D("0.00")))

    def test_metrics_endpoint(self):
        response = self.client.get("/api/metrics", {"rules_version": "v1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "rules_version": "v1", "n": 3, "ai_mae": 116.67, "ai_bias": -83.33,
            "baseline_name": "Regla simple", "baseline_description": "min(Amount of Claim, Max Benefit)",
            "baseline_mae": 233.33, "improvement_pct": 50.0,
        })

    def test_metrics_endpoint_without_data(self):
        response = self.client.get("/api/metrics", {"rules_version": "v9"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "rules_version": "v9", "n": 0, "ai_mae": None, "ai_bias": None,
            "baseline_name": "Regla simple", "baseline_description": "min(Amount of Claim, Max Benefit)",
            "baseline_mae": None, "improvement_pct": None,
        })

    def test_metrics_claims_endpoint(self):
        response = self.client.get("/api/metrics/claims", {"rules_version": "v1"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([r["tracking_number"] for r in body], ["G", "A", "B"])
        self.assertEqual(body[0], {"tracking_number": "G", "human": 900.0, "ai": 600.0, "difference": -300.0,
                                   "abs_error": 300.0, "route": "auto", "flags": ["claim_amount_mismatch"]})
        self.assertNotIn("b1", body[0])

    def test_metrics_claims_endpoint_without_data(self):
        self.assertEqual(self.client.get("/api/metrics/claims", {"rules_version": "v9"}).json(), [])

    def test_command(self):
        out = StringIO()
        call_command("evaluate", "--rules-version", "v1", stdout=out)
        self.assertEqual(
            out.getvalue().strip(),
            "IA: MAE $116.67 | Regla simple: MAE $233.33 | 50.0% menos error | Bias IA: -$83.33 | n=3",
        )

    def test_command_without_data(self):
        out = StringIO()
        call_command("evaluate", "--rules-version", "v9", stdout=out)
        self.assertIn("Nada que evaluar en rules_version v9", out.getvalue())
