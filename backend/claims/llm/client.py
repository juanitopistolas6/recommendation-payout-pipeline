"""Única puerta de entrada a Gemini. Ningún otro módulo usa el SDK google-genai directamente.

Cada llamada pasa por: cuota diaria -> intervalo por minuto -> API (con reintentos) ->
validación Pydantic (1 reintento) -> registro en LLMCall.

Uso:
    from claims.llm.client import PromptFile, get_client, load_prompt, render_prompt

    prompt = render_prompt(load_prompt("lease"), address=claim.lease_street_address)
    result = get_client().generate(
        model=settings.GEMINI_MODEL_EXTRACT, purpose="lease", prompt=prompt,
        files=[PromptFile("/data/claims/1/lease.pdf")], schema=LeaseSchema, claim=claim,
    )
"""

from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TypeVar

import httpx
from django.conf import settings
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from claims.models import Claim, LLMCall

from .errors import DailyQuotaExceeded, InvalidModelOutput, LLMError, LLMRequestError, LLMUnavailable
from .rate_limit import MinuteRateLimiter, check_daily_quota, minute_limiter, model_limits

Schema = TypeVar("Schema", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ---------------------------------------------------------------------------
# Plantillas de prompts
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load_prompt(name: str) -> str:
    """Lee claims/prompts/<name>.md tal cual."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render_prompt(template: str, **variables) -> str:
    """Reemplaza {{variable}} con texto simple. No usa str.format: los prompts traen llaves de JSON."""
    expected = set(_PLACEHOLDER.findall(template))
    missing = expected - variables.keys()
    unknown = variables.keys() - expected
    if missing or unknown:
        raise ValueError(f"Variables del prompt no coinciden. Faltan: {sorted(missing)}. Sobran: {sorted(unknown)}")
    return _PLACEHOLDER.sub(lambda m: str(variables[m.group(1)]), template)


# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------

_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
}


@dataclass(frozen=True)
class PromptFile:
    """Un documento que se manda a Gemini como parte binaria (PDF o imagen), no como texto extraído."""

    path: str
    mime_type: str | None = None
    label: str | None = None  # texto que va justo antes del archivo, ej. "Document ID: 12"

    @property
    def resolved_mime_type(self) -> str:
        if self.mime_type:
            return self.mime_type
        suffix = Path(self.path).suffix.lower()
        mime = _MIME_BY_EXTENSION.get(suffix) or mimetypes.guess_type(self.path)[0]
        if not mime:
            raise LLMRequestError(f"No se pudo determinar el mime_type de {self.path}")
        return mime


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

@dataclass
class RawResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class _Retryable(Exception):
    """Envuelve un error recuperable (429 por minuto, 5xx, red) para tenacity."""

    def __init__(self, original: Exception):
        self.original = original
        super().__init__(str(original))


class _InvalidOutput(Exception):
    def __init__(self, raw_text: str, error: ValidationError):
        self.raw_text = raw_text
        self.error = error
        super().__init__(str(error))


class GeminiClient:
    def __init__(self, api_key: str | None = None, limiter: MinuteRateLimiter | None = None, sleep=time.sleep):
        self._api_key = api_key if api_key is not None else settings.GEMINI_API_KEY
        self._limiter = limiter or minute_limiter
        self._sleep = sleep  # espera entre reintentos (se reemplaza en tests)
        self._sdk = None

    # -- API pública --------------------------------------------------------

    def generate(
        self,
        model: str,
        purpose: str,
        prompt: str,
        files: Iterable[PromptFile | str] = (),
        schema: type[Schema] = None,
        claim: Claim | None = None,
    ) -> Schema:
        if schema is None:
            raise ValueError("schema es obligatorio: toda respuesta se valida con un modelo Pydantic")
        if purpose not in LLMCall.Purpose.values:
            raise ValueError(f"purpose inválido: {purpose!r}. Opciones: {LLMCall.Purpose.values}")
        files = [f if isinstance(f, PromptFile) else PromptFile(str(f)) for f in files]
        limits = model_limits(model)
        self._ensure_ready()  # sin API key no se intenta nada, ni se registra una llamada que no ocurrió

        try:
            return self._call_with_retries(model, purpose, prompt, files, schema, claim, limits)
        except _InvalidOutput:
            pass  # 1 reintento ante salida que no valida
        try:
            return self._call_with_retries(model, purpose, prompt, files, schema, claim, limits)
        except _InvalidOutput as exc:
            raise InvalidModelOutput(model, exc.raw_text, exc.error) from exc.error

    # -- Reintentos ----------------------------------------------------------

    def _call_with_retries(self, model, purpose, prompt, files, schema, claim, limits):
        retrying = Retrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(settings.GEMINI_RETRY_ATTEMPTS),
            wait=wait_exponential(
                multiplier=settings.GEMINI_RETRY_WAIT_MIN,
                min=settings.GEMINI_RETRY_WAIT_MIN,
                max=settings.GEMINI_RETRY_WAIT_MAX,
            ),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retrying(self._attempt, model, purpose, prompt, files, schema, claim, limits)
        except _Retryable as exc:
            raise LLMUnavailable(
                f"{model}: se agotaron {settings.GEMINI_RETRY_ATTEMPTS} intentos. Último error: {exc.original}"
            ) from exc.original

    def _attempt(self, model, purpose, prompt, files, schema, claim, limits):
        """Un único request a la API, con su control de cuota y su registro en LLMCall."""
        log_model = self.quota_model_name(model)
        check_daily_quota(log_model, limits["rpd"], settings.GEMINI_DAILY_QUOTA_THRESHOLD)
        self._limiter.wait(log_model, limits["rpm"], settings.GEMINI_RPM_MARGIN)

        started = time.monotonic()
        try:
            raw = self._call_api(model, purpose, prompt, files, schema)
        except Exception as exc:
            status, kind = self._classify_error(exc)
            self._log(log_model, purpose, claim, status, started, error_detail=_describe(exc))
            if kind == "daily":
                raise DailyQuotaExceeded(model, detail=_describe(exc)) from exc
            if kind == "retry":
                raise _Retryable(exc) from exc
            if isinstance(exc, LLMError):
                raise
            raise LLMRequestError(f"{model}: {_describe(exc)}") from exc

        try:
            parsed = schema.model_validate_json(raw.text)
        except ValidationError as exc:
            self._log(log_model, purpose, claim, LLMCall.Status.INVALID_OUTPUT, started, raw,
                      error_detail=f"{exc}\n--- respuesta ---\n{raw.text[:2000]}")
            raise _InvalidOutput(raw.text, exc) from exc

        self._log(log_model, purpose, claim, LLMCall.Status.OK, started, raw)
        return parsed

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, str]:
        """(status para LLMCall, tipo): 'daily' | 'retry' | 'fatal'."""
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.APIError):
            if exc.code == 429:
                if "PerDay" in f"{exc.message} {exc.details}":
                    return LLMCall.Status.RATE_LIMITED_DAY, "daily"
                return LLMCall.Status.RATE_LIMITED_MINUTE, "retry"
            if isinstance(exc, genai_errors.ServerError):
                return LLMCall.Status.ERROR, "retry"
            return LLMCall.Status.ERROR, "fatal"
        if isinstance(exc, httpx.TransportError):  # incluye timeouts y errores de conexión
            return LLMCall.Status.ERROR, "retry"
        return LLMCall.Status.ERROR, "fatal"

    # -- Llamada real al SDK ------------------------------------------------

    def quota_model_name(self, model: str) -> str:
        """Nombre con el que se registra y se cuenta la cuota del modelo."""
        return model

    def _ensure_ready(self) -> None:
        self._get_sdk()

    def _get_sdk(self):
        if self._sdk is None:
            if not self._api_key or self._api_key == "your-gemini-api-key":
                raise LLMRequestError("Falta GEMINI_API_KEY en .env")
            from google import genai

            self._sdk = genai.Client(api_key=self._api_key)
        return self._sdk

    def _call_api(self, model, purpose, prompt, files, schema) -> RawResponse:
        from google.genai import types

        sdk = self._get_sdk()
        uploaded = []
        try:
            total_bytes = sum(Path(f.path).stat().st_size for f in files)
            if total_bytes > settings.GEMINI_INLINE_MAX_BYTES:
                # Request demasiado grande para mandarlo inline: los archivos van por la Files API
                for f in files:
                    uploaded.append(sdk.files.upload(file=f.path, config={"mime_type": f.resolved_mime_type}))
                parts = list(uploaded)
            else:
                parts = [
                    types.Part.from_bytes(data=Path(f.path).read_bytes(), mime_type=f.resolved_mime_type)
                    for f in files
                ]

            contents = []
            for f, part in zip(files, parts):
                if f.label:
                    contents.append(f.label)
                contents.append(part)
            contents.append(prompt)

            response = sdk.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=schema,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                ),
            )
        finally:
            for f in uploaded:
                try:
                    sdk.files.delete(name=f.name)
                except Exception:  # noqa: BLE001 — expiran solos a las 48 h
                    pass

        usage = response.usage_metadata
        output_tokens = None
        if usage is not None and usage.candidates_token_count is not None:
            # Los tokens de razonamiento se facturan como salida
            output_tokens = usage.candidates_token_count + (usage.thoughts_token_count or 0)
        return RawResponse(
            text=response.text or "",
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=output_tokens,
        )

    # -- Registro -------------------------------------------------------------

    @staticmethod
    def _log(model, purpose, claim, status, started, raw: RawResponse | None = None, error_detail: str = ""):
        LLMCall.objects.create(
            model=model,
            purpose=purpose,
            claim=claim,
            input_tokens=raw.input_tokens if raw else None,
            output_tokens=raw.output_tokens if raw else None,
            status=status,
            error_detail=error_detail,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def get_client() -> GeminiClient:
    """GeminiClient real, o FakeGeminiClient si GEMINI_FAKE=1."""
    if settings.GEMINI_FAKE:
        from .fake import FakeGeminiClient

        return FakeGeminiClient()
    return GeminiClient()
