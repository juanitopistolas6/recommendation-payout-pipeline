"""Errores de la capa LLM. El resto del pipeline solo captura estos, nunca los del SDK."""


class LLMError(Exception):
    """Base de todos los errores de la capa LLM."""


class DailyQuotaExceeded(LLMError):
    """Se alcanzó (o se está por alcanzar) la cuota diaria del modelo. No se reintenta hasta mañana."""

    def __init__(self, model: str, used: int | None = None, limit: int | None = None, detail: str = ""):
        self.model = model
        self.used = used
        self.limit = limit
        msg = f"Cuota diaria agotada para {model}"
        if used is not None and limit is not None:
            msg += f" ({used}/{limit} llamadas hoy)"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class InvalidModelOutput(LLMError):
    """La respuesta no validó contra el esquema Pydantic, tampoco en el reintento."""

    def __init__(self, model: str, raw_text: str, validation_error: Exception):
        self.model = model
        self.raw_text = raw_text
        self.validation_error = validation_error
        super().__init__(f"Salida inválida de {model}: {validation_error}")


class LLMUnavailable(LLMError):
    """Se agotaron los reintentos ante 429 por minuto, 5xx o errores de red."""


class LLMRequestError(LLMError):
    """Error no recuperable de la API (p. ej. 400: request mal formado, archivo no soportado)."""


class LLMStageNotConfigured(LLMError):
    """Falta un prompt (claims/prompts/*.md) o su esquema en claims/llm/schemas.py, ambos del apéndice."""
