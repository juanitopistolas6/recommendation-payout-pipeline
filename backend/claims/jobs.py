"""Cola de trabajos (RQ + Redis): procesa los claims en segundo plano, uno a la vez.

    status = enqueue_claim(claim)          # "queued" o el estado del job que ya existía
    position = queue_position("1013")      # 1 = el siguiente en procesarse; None si no está esperando

Hay un solo worker (docker compose: servicio `worker`). El cuello de botella es la cuota de Gemini.
"""

from __future__ import annotations

import django_rq
from django.conf import settings
from rq.job import JobStatus

from claims.llm.client import get_client
from claims.llm.errors import DailyQuotaExceeded
from claims.llm.rate_limit import check_daily_quota, model_limits
from claims.models import Claim, ClaimDocument
from claims.pipeline.orchestrator import process_claim, quota_reason

QUEUE_NAME = "default"
JOB_TIMEOUT = 600
_ACTIVE = (JobStatus.QUEUED, JobStatus.STARTED)


def job_id_for(tracking_number: str) -> str:
    return f"claim-{tracking_number}"


def get_queue():
    return django_rq.get_queue(QUEUE_NAME)


def enqueue_claim(claim: Claim, force_from_step: int | None = None, queue=None) -> str:
    """Encola el claim. Si ya está en cola o procesándose, no lo duplica y devuelve ese estado."""
    queue = queue or get_queue()
    job_id = job_id_for(claim.pk)

    existing = queue.fetch_job(job_id)
    if existing is not None:
        status = existing.get_status(refresh=True)
        if status in _ACTIVE:
            return str(status.value)

    # Se marca antes de encolar: si el worker lo toma al instante, su estado no se pisa
    claim.processing_status = Claim.ProcessingStatus.QUEUED
    claim.failure_reason = ""
    claim.save(update_fields=["processing_status", "failure_reason", "updated_at"])
    queue.enqueue(run_claim, claim.pk, force_from_step, job_id=job_id, job_timeout=JOB_TIMEOUT)
    return str(JobStatus.QUEUED.value)


def queue_position(tracking_number: str, queue=None) -> int | None:
    """Posición en la cola, empezando en 1. None si no está esperando (ya empezó, terminó o no existe)."""
    queue = queue or get_queue()
    position = queue.get_job_position(job_id_for(tracking_number))
    return None if position is None else position + 1


def run_claim(tracking_number: str, force_from_step: int | None = None) -> None:
    """Lo que ejecuta el worker. Antes de procesar revisa la cuota diaria: si un job anterior la agotó,
    marca el claim como quota_exhausted sin llamar a Gemini."""
    claim = Claim.objects.get(pk=tracking_number)
    blocked = quota_block_reason(claim)
    if blocked:
        claim.processing_status = Claim.ProcessingStatus.QUOTA_EXHAUSTED
        claim.failure_reason = blocked
        claim.save(update_fields=["processing_status", "failure_reason", "updated_at"])
        return
    process_claim(tracking_number, force_from_step=force_from_step)


def quota_block_reason(claim: Claim) -> str:
    """Motivo si algún modelo que el claim va a necesitar ya agotó su cuota del día; "" si puede seguir."""
    models = [settings.GEMINI_MODEL_EXTRACT]  # contrato y claim: lo usan todos
    if claim.documents.filter(doc_type__in=[ClaimDocument.DocType.PENDING, ClaimDocument.DocType.UNKNOWN]).exists():
        models.insert(0, settings.GEMINI_MODEL_CLASSIFY)  # solo si hay algo que clasificar con el LLM

    client = get_client()
    for model in models:
        try:
            check_daily_quota(client.quota_model_name(model), model_limits(model)["rpd"],
                              settings.GEMINI_DAILY_QUOTA_THRESHOLD)
        except DailyQuotaExceeded as exc:
            exc.model = model
            return quota_reason(exc)
    return ""
