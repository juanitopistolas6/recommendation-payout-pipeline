"""GET /api/claims/stream?ids=1,1045 — estados de claims en vivo (Server-Sent Events).

Vista async normal de Django (no ninja). Necesita un servidor ASGI (uvicorn): con runserver (WSGI)
Django consume el generador completo antes de enviar nada y los eventos llegarían todos al final.

Protocolo:
    event: claim     -> al conectarse, el estado de cada ID (snapshot); después, solo los que cambian
    : keep-alive     -> en cada ciclo
    event: complete  -> todos en estado final; se cierra
    event: timeout   -> se alcanzó la duración máxima; se cierra (el cliente puede reconectarse)
"""

from __future__ import annotations

import asyncio
import json
import time

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponseBadRequest, StreamingHttpResponse

from claims import jobs
from claims.api import FINAL_STATUSES, MAX_IDS, latest_recommendation, normalize_ids, recommendation_summary
from claims.models import Claim

POLL_SECONDS = 1.5
MAX_SECONDS = 15 * 60


def get_states(ids: list[str]) -> dict[str, dict]:
    """Estado actual de cada ID: processing_status, updated_at y el resumen de la recomendación (BD),
    y la posición en la cola (Redis) solo para los que están en cola."""
    claims = Claim.objects.in_bulk(ids)
    queue = jobs.get_queue()  # no se conecta a Redis hasta la primera consulta
    states = {}
    for tracking in ids:
        claim = claims.get(tracking)
        if claim is None:
            states[tracking] = {"tracking_number": tracking, "status": "not_found", "updated_at": None}
            continue
        states[tracking] = {
            "tracking_number": tracking,
            "status": claim.processing_status,
            "updated_at": claim.updated_at.isoformat(),
            "failure_reason": claim.failure_reason or None,
            "summary": recommendation_summary(latest_recommendation(claim)),
            "queue_position": (jobs.queue_position(tracking, queue=queue)
                               if claim.processing_status == Claim.ProcessingStatus.QUEUED else None),
        }
    return states


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


async def _events(ids: list[str], poll_seconds: float = POLL_SECONDS, max_seconds: float = MAX_SECONDS):
    read_states = sync_to_async(get_states)
    started = time.monotonic()

    states = await read_states(ids)
    for state in states.values():  # snapshot: tras una reconexión no se pierde nada
        yield _event("claim", state)

    while True:
        if all(s["status"] in FINAL_STATUSES for s in states.values()):
            yield _event("complete", {"ids": ids})
            return
        if time.monotonic() - started >= max_seconds:
            yield _event("timeout", {"ids": ids, "seconds": max_seconds})
            return

        yield ": keep-alive\n\n"
        await asyncio.sleep(poll_seconds)

        current = await read_states(ids)
        for tracking, state in current.items():
            # La posición en la cola avanza sin que cambie el claim: también cuenta como cambio
            previous = states[tracking]
            if (state["updated_at"], state.get("queue_position")) != (previous["updated_at"], previous.get("queue_position")):
                yield _event("claim", state)
        states = current


async def claims_stream(request: HttpRequest):
    if request.method != "GET":
        return HttpResponseBadRequest("Solo GET")
    ids = normalize_ids(request.GET.get("ids", "").split(","))
    if not ids:
        return HttpResponseBadRequest("Falta ?ids=1,2,3")
    if len(ids) > MAX_IDS:
        return HttpResponseBadRequest(f"Máximo {MAX_IDS} IDs")

    response = StreamingHttpResponse(_events(ids), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
