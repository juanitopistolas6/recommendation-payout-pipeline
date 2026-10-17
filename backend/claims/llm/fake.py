"""Cliente falso: mismas reglas que GeminiClient (cuota, intervalo, validación, LLMCall), sin llamar a la API.

Se activa con GEMINI_FAKE=1. Lee la respuesta de <responses_dir>/<purpose>.json:

    {"document_type": "lease"}                 -> siempre devuelve ese objeto
    [ "no es json", {"document_type": "lease"} ] -> una por llamada; la última se repite
    [ {"__error__": {"code": 429, "response": {...}}} ] -> lanza el error de la API con ese cuerpo

Los registros en LLMCall llevan el prefijo "fake/" en el modelo, así nunca cuentan para la cuota real.
"""

import json
from collections import Counter
from pathlib import Path

from django.conf import settings

from .client import GeminiClient, RawResponse


class FakeGeminiClient(GeminiClient):
    def __init__(self, responses_dir: str | Path | None = None, **kwargs):
        super().__init__(api_key="", **kwargs)
        self.responses_dir = Path(responses_dir or settings.GEMINI_FAKE_RESPONSES_DIR)
        self.calls: list[dict] = []  # lo que habría llegado a la API, para inspeccionarlo en tests
        self._served = Counter()

    def _ensure_ready(self) -> None:
        pass  # no necesita API key

    def quota_model_name(self, model: str) -> str:
        return f"fake/{model}"

    def _call_api(self, model, purpose, prompt, files, schema) -> RawResponse:
        self.calls.append({"model": model, "purpose": purpose, "prompt": prompt, "files": list(files)})

        path = self.responses_dir / f"{purpose}.json"
        if not path.exists():
            raise FileNotFoundError(f"FakeGeminiClient: no existe la respuesta fija {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, list):
            item = data[min(self._served[purpose], len(data) - 1)]
        else:
            item = data
        self._served[purpose] += 1

        if isinstance(item, dict) and "__error__" in item:
            raise _api_error(**item["__error__"])
        text = item if isinstance(item, str) else json.dumps(item)
        return RawResponse(text=text, input_tokens=0, output_tokens=0)


def _api_error(code: int, response: dict | None = None):
    from google.genai import errors

    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, response or {"error": {"code": code, "message": f"fake {code}"}})
