"""API de claims (django-ninja).

    POST /api/claims/analyze           -> encola y responde 202 al instante
    GET  /api/claims/{tracking_number} -> detalle para la vista del frontend
    GET  /api/metrics                  -> evaluación: MAE de la IA vs. la regla simple
    GET  /api/metrics/claims           -> evaluación: una fila por claim

El streaming en vivo (SSE) está en claims/sse.py: es una vista normal de Django.
Los endpoints de claims no exponen nada de ClaimOutcome. Los de métricas sí (es su propósito), pero solo
a través de claims/evaluation/metrics.py, el único módulo que lo lee.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field
from rq.job import JobStatus

from claims import jobs
from claims.evaluation import metrics
from claims.models import Claim, Recommendation
from claims.pipeline.rules import RULES_VERSION

router = Router(tags=["claims"])
metrics_router = Router(tags=["metrics"])

MAX_IDS = 15
FINAL_STATUSES = {"done", "needs_review", "failed", "quota_exhausted", "not_found"}


def normalize_ids(raw) -> list[str]:
    """Quita espacios, vacíos y duplicados, conservando el orden."""
    return list(dict.fromkeys(str(i).strip() for i in raw if str(i).strip()))


def latest_recommendation(claim: Claim) -> Recommendation | None:
    return claim.recommendations.order_by("-created_at").first()


def recommendation_summary(rec: Recommendation | None) -> dict | None:
    if rec is None:
        return None
    return {
        "recommended_amount": str(rec.recommended_amount) if rec.recommended_amount is not None else None,
        "route": rec.route,
        "flags": rec.flags,
        "cap_applied": rec.cap_applied,
    }


# ---------------------------------------------------------------------------
# POST /api/claims/analyze
# ---------------------------------------------------------------------------

class AnalyzeIn(Schema):
    tracking_numbers: list[str] = Field(..., min_length=1)
    force: bool = False


class Summary(Schema):
    recommended_amount: Decimal | None
    route: str
    flags: list[str]
    cap_applied: bool


class AnalyzeItem(Schema):
    tracking_number: str
    status: str
    queue_position: int | None = None
    summary: Summary | None = None
    failure_reason: str | None = None


@router.post("/analyze", response={202: list[AnalyzeItem]})
def analyze(request, payload: AnalyzeIn):
    """Encola los claims pedidos y devuelve su estado al instante. Máximo 15 IDs (ya normalizados)."""
    ids = normalize_ids(payload.tracking_numbers)
    if not ids:
        raise HttpError(422, "No hay tracking numbers válidos")
    if len(ids) > MAX_IDS:
        raise HttpError(422, f"Máximo {MAX_IDS} tracking numbers por request (llegaron {len(ids)})")

    claims = Claim.objects.in_bulk(ids)
    queue = jobs.get_queue()
    return 202, [_analyze_one(tracking, claims.get(tracking), payload.force, queue) for tracking in ids]


def _analyze_one(tracking: str, claim: Claim | None, force: bool, queue) -> dict:
    if claim is None:
        return {"tracking_number": tracking, "status": "not_found"}

    # Ya en la cola o procesándose: no se toca, se informa dónde va
    job = queue.fetch_job(jobs.job_id_for(tracking))
    if job is not None and job.get_status(refresh=True) in (JobStatus.QUEUED, JobStatus.STARTED):
        return {"tracking_number": tracking, "status": claim.processing_status,
                "queue_position": jobs.queue_position(tracking, queue=queue)}

    if not force:
        rec = latest_recommendation(claim)
        if rec is not None and claim.processing_status in FINAL_STATUSES:
            return {"tracking_number": tracking, "status": claim.processing_status,
                    "summary": recommendation_summary(rec)}
        if claim.processing_status == Claim.ProcessingStatus.FAILED:
            # Un claim fallido no se reintenta solo: hay que pedirlo con force
            return {"tracking_number": tracking, "status": claim.processing_status,
                    "failure_reason": claim.failure_reason}

    status = jobs.enqueue_claim(claim, queue=queue)
    return {"tracking_number": tracking, "status": status, "queue_position": jobs.queue_position(tracking, queue=queue)}


# ---------------------------------------------------------------------------
# GET /api/claims/{tracking_number}
# ---------------------------------------------------------------------------

class ClaimData(Schema):
    tracking_number: str
    claim_date: date | None
    lease_street_address: str
    lease_city: str
    lease_state: str
    lease_zip: str
    lease_start_date: date | None
    lease_end_date: date | None
    move_out_date: date | None
    monthly_rent: Decimal | None
    termination_type: str
    claim_amount: Decimal | None
    max_benefit: Decimal | None
    policy: str
    property_management_company: str
    property_manager_name: str
    pm_explanation: str


class DocumentOut(Schema):
    id: int
    file_name: str
    doc_type: str
    classification_method: str
    classification_confidence: str
    page_count: int | None
    has_scanned_pages: bool


class RecommendationOut(Schema):
    recommended_amount: Decimal | None
    eligible_owed: Decimal | None
    excluded_total: Decimal | None
    cap_applied: bool
    explanation: str
    flags: list[str]
    route: str
    rules_version: str
    created_at: datetime
    charges: list[dict]       # cada cargo con elegibilidad, razón y saldo (Recommendation.reasoning)
    payments: list[dict]      # pagos, créditos y concesiones extraídos (Prompt B)
    findings: list | dict | None  # hallazgos del Prompt B, si los trae
    model_used: str
    extraction_confidence: str


class ClaimDetail(Schema):
    claim: ClaimData
    status: str
    failure_reason: str
    queue_position: int | None
    documents: list[DocumentOut]
    recommendation: RecommendationOut | None


_CLAIM_FIELDS = [f for f in ClaimData.model_fields if f != "tracking_number"]


@router.get("/{tracking_number}", response=ClaimDetail)
def claim_detail(request, tracking_number: str):
    """Detalle del claim: solo datos de Claim, sus documentos y la última recomendación."""
    claim = get_object_or_404(Claim, pk=tracking_number.strip())
    rec = latest_recommendation(claim)
    return {
        "claim": {"tracking_number": claim.pk, **{f: getattr(claim, f) for f in _CLAIM_FIELDS}},
        "status": claim.processing_status,
        "failure_reason": claim.failure_reason,
        "queue_position": jobs.queue_position(claim.pk),
        "documents": list(claim.documents.order_by("id")),
        "recommendation": _recommendation_detail(rec),
    }


def _recommendation_detail(rec: Recommendation | None) -> dict | None:
    if rec is None:
        return None
    raw = rec.extraction.raw_json or {}
    return {
        "recommended_amount": rec.recommended_amount,
        "eligible_owed": rec.eligible_owed,
        "excluded_total": rec.excluded_total,
        "cap_applied": rec.cap_applied,
        "explanation": rec.explanation,
        "flags": rec.flags,
        "route": rec.route,
        "rules_version": rec.rules_version,
        "created_at": rec.created_at,
        "charges": rec.reasoning,
        "payments": raw.get("payments_and_credits") or [],
        "findings": raw.get("findings"),
        "model_used": rec.extraction.model_used,
        "extraction_confidence": rec.extraction.confidence,
    }


# ---------------------------------------------------------------------------
# GET /api/metrics y /api/metrics/claims
# ---------------------------------------------------------------------------

# Montos como números JSON (no strings como en el resto de la API): el dashboard solo los muestra y compara.

class MetricsOut(Schema):
    rules_version: str
    n: int
    ai_mae: float | None
    ai_bias: float | None              # mean(ia - humano): negativo = la IA paga menos que el humano
    baseline_name: str
    baseline_description: str
    baseline_mae: float | None
    improvement_pct: float | None      # (baseline_mae - ai_mae) / baseline_mae × 100; negativo = la IA es peor


class MetricsClaimRow(Schema):
    tracking_number: str
    human: float
    ai: float
    difference: float                  # ai - humano
    abs_error: float
    route: str
    flags: list[str]


@metrics_router.get("", response=MetricsOut)
def metrics_summary(request, rules_version: str = RULES_VERSION):
    """IA vs. la regla simple min(claim_amount, max_benefit), sobre los mismos claims, contra approved_benefit.
    Con n = 0 responde 200 con las métricas en null."""
    return metrics.evaluate(rules_version)


@metrics_router.get("/claims", response=list[MetricsClaimRow])
def metrics_claims(request, rules_version: str = RULES_VERSION):
    """Una fila por claim evaluado, del mayor error absoluto de la IA al menor."""
    return metrics.claim_rows(rules_version)
