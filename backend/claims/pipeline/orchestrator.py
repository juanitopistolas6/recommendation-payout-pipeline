"""Procesa un claim de punta a punta, actualiza su processing_status y maneja cada error según su tipo.

Pasos:
    1. classifying      -> classify_claim_documents
    2. analyzing_lease  -> analyze_lease
    3. analyzing_claim  -> analyze_claim
    4. computing        -> build_recommendation (se guarda)
       Si la ruta es retry_stronger_model: repite el paso 3 una vez con el modelo de reintento y recalcula.

Estado final: done (ruta auto) o needs_review. Un claim en failed nunca se reintenta automáticamente.
"""

from __future__ import annotations

import logging
import traceback
from datetime import timedelta, timezone

from django.conf import settings

from claims.llm.errors import DailyQuotaExceeded, InvalidModelOutput, LLMError
from claims.llm.rate_limit import quota_day_start
from claims.models import Claim, ClaimDocument, Recommendation
from claims.pipeline.claim import analyze_claim
from claims.pipeline.classify import classify_claim_documents
from claims.pipeline.lease import analyze_lease
from claims.pipeline.rules import build_recommendation

logger = logging.getLogger(__name__)

Status = Claim.ProcessingStatus

STEP_CLASSIFY, STEP_LEASE, STEP_CLAIM, STEP_COMPUTE = 1, 2, 3, 4
NO_DOCUMENTS = "Documents not found for this claim."

_REC_FIELDS = ("extraction", "recommended_amount", "eligible_owed", "excluded_total", "cap_applied",
               "flags", "reasoning", "explanation", "route")


def process_claim(tracking_number: str, force_from_step: int | None = None, client=None) -> None:
    """`force_from_step=N` ignora la caché desde el paso N en adelante. `client` solo se pasa en tests."""
    claim = Claim.objects.get(pk=tracking_number)
    force = lambda step: force_from_step is not None and step >= force_from_step  # noqa: E731

    if not claim.documents.exists():
        _set_status(claim, Status.FAILED, NO_DOCUMENTS)
        return

    try:
        _set_status(claim, Status.CLASSIFYING)
        if force(STEP_CLASSIFY):
            _reset_classification(claim)
        classify_claim_documents(claim, client=client)

        _set_status(claim, Status.ANALYZING_LEASE)
        lease = analyze_lease(claim, client=client, force=force(STEP_LEASE))

        _set_status(claim, Status.ANALYZING_CLAIM)
        extraction = analyze_claim(claim, lease, client=client, force=force(STEP_CLAIM))

        _set_status(claim, Status.COMPUTING)
        recommendation = build_recommendation(claim, extraction, lease)

        if recommendation.route == Recommendation.Route.RETRY_STRONGER_MODEL:
            _set_status(claim, Status.ANALYZING_CLAIM)
            extraction = analyze_claim(claim, lease, model=settings.GEMINI_MODEL_RETRY, client=client,
                                       force=force(STEP_CLAIM))
            _set_status(claim, Status.COMPUTING)
            recommendation = build_recommendation(claim, extraction, lease)
            if recommendation.route == Recommendation.Route.RETRY_STRONGER_MODEL:
                recommendation.route = Recommendation.Route.REVIEW  # el modelo fuerte tampoco lo resolvió

        _save_recommendation(recommendation)
        final = Status.DONE if recommendation.route == Recommendation.Route.AUTO else Status.NEEDS_REVIEW
        _set_status(claim, final)

    except DailyQuotaExceeded as exc:
        _set_status(claim, Status.QUOTA_EXHAUSTED, quota_reason(exc))
        raise  # la cola (Sección 9) lo necesita para pausar ese modelo
    except InvalidModelOutput as exc:
        _set_status(claim, Status.FAILED, f"Salida inválida del modelo {exc.model}: {exc.validation_error}")
    except LLMError as exc:
        _set_status(claim, Status.FAILED, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error inesperado procesando el claim %s", tracking_number)
        _set_status(claim, Status.FAILED, _summarize_traceback(exc))


# ---------------------------------------------------------------------------

def _set_status(claim: Claim, status: str, reason: str = "") -> None:
    claim.processing_status = status
    claim.failure_reason = reason
    claim.save(update_fields=["processing_status", "failure_reason", "updated_at"])


def _reset_classification(claim: Claim) -> None:
    """Vuelve a clasificar todo menos lo que se corrigió a mano."""
    claim.documents.exclude(classification_method=ClaimDocument.ClassificationMethod.MANUAL).update(
        doc_type=ClaimDocument.DocType.PENDING, classification_method="", classification_confidence="",
    )


def _save_recommendation(rec: Recommendation) -> None:
    """Una recomendación por (claim, RULES_VERSION): se actualiza si ya existe."""
    Recommendation.objects.update_or_create(
        claim=rec.claim, rules_version=rec.rules_version,
        defaults={field: getattr(rec, field) for field in _REC_FIELDS},
    )


def quota_reason(exc: DailyQuotaExceeded) -> str:
    reset = quota_day_start() + timedelta(days=1)
    used = f" ({exc.used}/{exc.limit} llamadas)" if exc.used is not None else ""
    return (f"Cuota diaria agotada de {exc.model}{used}. Se reinicia a la medianoche del Pacífico: "
            f"{reset:%Y-%m-%d %H:%M %Z} ({reset.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC).")


def _summarize_traceback(exc: Exception) -> str:
    """Tipo, mensaje, el paso del pipeline donde ocurrió y los 2 frames donde se originó."""
    tb = traceback.extract_tb(exc.__traceback__)
    frames = tb if len(tb) <= 3 else [tb[0], *tb[-2:]]
    where = " -> ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in frames)
    return f"{type(exc).__name__}: {exc} [{where}]"
