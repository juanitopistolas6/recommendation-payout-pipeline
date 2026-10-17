"""Una sola llamada real a Gemini para verificar la capa LLM de punta a punta.

Uso:
    docker compose exec backend python manage.py llm_smoke_test "/data/claims/1/305 Coosawatchie St move out calculation - hall.pdf"
    docker compose exec backend python manage.py llm_smoke_test <archivo> --model gemini-3.5-flash-lite

Gasta 1 request de la cuota diaria del modelo (2 si la respuesta no valida y se reintenta).
El esquema y el prompt son solo de prueba: no son los del pipeline.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pydantic import BaseModel

from claims.llm.client import GeminiClient, PromptFile
from claims.llm.errors import LLMError
from claims.llm.rate_limit import calls_today, model_limits
from claims.models import LLMCall


class SmokeTestResult(BaseModel):
    document_kind: str
    page_count: int
    mentions_a_dollar_amount: bool


PROMPT = (
    "Look at the attached document. Reply with JSON: document_kind (a short label such as "
    "'lease', 'ledger', 'invoice', 'letter'), page_count (number of pages you received) and "
    "mentions_a_dollar_amount (true if any dollar amount appears)."
)


class Command(BaseCommand):
    help = "Hace UNA llamada real a Gemini con un archivo y muestra el resultado validado y su LLMCall."

    def add_arguments(self, parser):
        parser.add_argument("path", help="PDF o imagen dentro del contenedor (ej. /data/claims/1/archivo.pdf)")
        parser.add_argument("--model", default=settings.GEMINI_MODEL_CLASSIFY)

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.is_file():
            raise CommandError(f"No existe el archivo: {path}")
        model = opts["model"]
        try:
            limits = model_limits(model)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Modelo: {model} — usadas hoy: {calls_today(model)}/{limits['rpd']}")
        last_id = LLMCall.objects.order_by("-id").values_list("id", flat=True).first() or 0

        try:
            result = GeminiClient().generate(
                model=model, purpose="classify", prompt=PROMPT, files=[PromptFile(str(path))], schema=SmokeTestResult
            )
        except LLMError as exc:
            self._print_calls(last_id)
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Respuesta validada: {result.model_dump_json()}"))
        self._print_calls(last_id)

    def _print_calls(self, after_id):
        for call in LLMCall.objects.filter(id__gt=after_id).order_by("id"):
            self.stdout.write(
                f"LLMCall #{call.id}: status={call.status} input_tokens={call.input_tokens} "
                f"output_tokens={call.output_tokens} latency_ms={call.latency_ms}"
                + (f"\n  {call.error_detail[:300]}" if call.error_detail else "")
            )
