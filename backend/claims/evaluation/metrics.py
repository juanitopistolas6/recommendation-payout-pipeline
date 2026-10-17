"""Evaluación: qué tan cerca está la IA de la decisión humana, comparada con una regla simple.

Es el ÚNICO módulo que lee ClaimOutcome (approved_benefit = la decisión humana). Nada de aquí entra a un prompt.

Conjunto evaluado: claims con una Recommendation de la rules_version pedida y con approved_benefit no nulo.
La IA y la regla simple se miden sobre exactamente los mismos claims:

    IA            recommended_amount
    Regla simple  min(claim_amount, max_benefit)

Estructura:
    - Métricas puras (mae, bias, improvement_pct): listas de Decimal, sin BD. Se prueban con números a mano.
    - baseline: la regla simple, pura.
    - load_rows / evaluate / claim_rows: leen la BD y arman el reporte.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from claims.models import Recommendation
from claims.pipeline.rules import RULES_VERSION

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
TENTH = Decimal("0.1")

BASELINE_NAME = "Regla simple"
BASELINE_DESCRIPTION = "min(Amount of Claim, Max Benefit)"


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Métricas puras. `preds` y `humans` van en el mismo orden y tienen el mismo largo.
# Con listas vacías devuelven None.
# ---------------------------------------------------------------------------

def mae(preds: list[Decimal], humans: list[Decimal]) -> Decimal | None:
    """mean(|pred - humano|)"""
    errors = [abs(p - h) for p, h in zip(preds, humans, strict=True)]
    return _round(sum(errors, ZERO) / len(errors)) if errors else None


def bias(preds: list[Decimal], humans: list[Decimal]) -> Decimal | None:
    """mean(pred - humano). Positivo = paga de más que el humano; negativo = paga de menos."""
    if not preds:
        return None
    return _round(sum((p - h for p, h in zip(preds, humans, strict=True)), ZERO) / len(preds))


def improvement_pct(baseline_mae: Decimal | None, ai_mae: Decimal | None) -> Decimal | None:
    """(baseline_mae - ai_mae) / baseline_mae × 100, a 1 decimal. Negativo = la IA se equivoca más que la regla.
    None si falta alguno de los dos o si el baseline no tiene error (no hay nada que mejorar)."""
    if baseline_mae is None or ai_mae is None or baseline_mae == 0:
        return None
    return ((baseline_mae - ai_mae) / baseline_mae * 100).quantize(TENTH, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Regla simple. Sin max_benefit no hay tope; nunca predice un pago negativo.
# ---------------------------------------------------------------------------

def baseline(claim_amount: Decimal, max_benefit: Decimal | None) -> Decimal:
    capped = claim_amount if max_benefit is None else min(claim_amount, max_benefit)
    return max(ZERO, capped)


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalRow:
    tracking_number: str
    human: Decimal
    ai: Decimal
    baseline: Decimal
    route: str
    flags: list[str]


def load_rows(rules_version: str = RULES_VERSION) -> list[EvalRow]:
    """Claims con Recommendation de `rules_version` y approved_benefit no nulo.

    Se saltan los que no tienen claim_amount o recommended_amount: sin ellos falta la predicción de la regla
    o de la IA, y la comparación dejaría de ser sobre los mismos claims.
    """
    recs = (
        Recommendation.objects
        .filter(rules_version=rules_version, claim__outcome__approved_benefit__isnull=False,
                claim__claim_amount__isnull=False, recommended_amount__isnull=False)
        .select_related("claim", "claim__outcome")
        .order_by("claim_id")
    )
    return [
        EvalRow(
            tracking_number=rec.claim.pk,
            human=rec.claim.outcome.approved_benefit,
            ai=rec.recommended_amount,
            baseline=baseline(rec.claim.claim_amount, rec.claim.max_benefit),
            route=rec.route,
            flags=list(rec.flags or []),
        )
        for rec in recs
    ]


def evaluate(rules_version: str = RULES_VERSION) -> dict:
    """MAE de la IA y de la regla simple, cuánto mejora la IA y su bias. Con n = 0, todo en None."""
    rows = load_rows(rules_version)
    humans = [r.human for r in rows]
    ai_mae = mae([r.ai for r in rows], humans)
    baseline_mae = mae([r.baseline for r in rows], humans)
    return {
        "rules_version": rules_version,
        "n": len(rows),
        "ai_mae": ai_mae,
        "ai_bias": bias([r.ai for r in rows], humans),
        "baseline_name": BASELINE_NAME,
        "baseline_description": BASELINE_DESCRIPTION,
        "baseline_mae": baseline_mae,
        "improvement_pct": improvement_pct(baseline_mae, ai_mae),
    }


def claim_rows(rules_version: str = RULES_VERSION) -> list[dict]:
    """Una fila por claim, del mayor error absoluto de la IA al menor. difference = ai - humano."""
    rows = [
        {
            "tracking_number": r.tracking_number,
            "human": r.human,
            "ai": r.ai,
            "difference": r.ai - r.human,
            "abs_error": abs(r.ai - r.human),
            "route": r.route,
            "flags": r.flags,
        }
        for r in load_rows(rules_version)
    ]
    return sorted(rows, key=lambda row: (-row["abs_error"], row["tracking_number"]))
