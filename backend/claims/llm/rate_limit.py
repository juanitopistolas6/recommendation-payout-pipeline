"""Control de cuota de Gemini: intervalo mínimo por minuto (en memoria) y contador diario (en LLMCall)."""

import math
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from claims.models import LLMCall

from .errors import DailyQuotaExceeded


def model_limits(model: str) -> dict:
    try:
        return settings.GEMINI_RATE_LIMITS[model]
    except KeyError:
        raise ValueError(f"Modelo sin límites configurados en GEMINI_RATE_LIMITS: {model}") from None


# ---------------------------------------------------------------------------
# Por minuto
# ---------------------------------------------------------------------------

class MinuteRateLimiter:
    """Garantiza un intervalo mínimo entre llamadas al mismo modelo.

    Vive en memoria del proceso: alcanza porque hay un solo worker.
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._last_call: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def interval(rpm: int, margin: float) -> float:
        return 60 / rpm * (1 + margin)

    def wait(self, model: str, rpm: int, margin: float) -> float:
        """Duerme lo necesario antes de llamar a `model`. Devuelve los segundos esperados."""
        with self._guard:
            lock = self._locks.setdefault(model, threading.Lock())
        with lock:
            waited = 0.0
            last = self._last_call.get(model)
            if last is not None:
                remaining = last + self.interval(rpm, margin) - self._clock()
                if remaining > 0:
                    self._sleep(remaining)
                    waited = remaining
            self._last_call[model] = self._clock()
            return waited


minute_limiter = MinuteRateLimiter()


# ---------------------------------------------------------------------------
# Por día
# ---------------------------------------------------------------------------

# Respuestas 429 no consumen cuota: no cuentan para el límite diario
_NOT_BILLED = (LLMCall.Status.RATE_LIMITED_MINUTE, LLMCall.Status.RATE_LIMITED_DAY)


def quota_day_start(now: datetime | None = None) -> datetime:
    """Última medianoche en la zona horaria de la cuota (America/Los_Angeles), como datetime aware."""
    tz = ZoneInfo(settings.GEMINI_QUOTA_TIMEZONE)
    local_now = (now or timezone.now()).astimezone(tz)
    return datetime.combine(local_now.date(), dtime.min, tzinfo=tz)


def calls_today(model: str, now: datetime | None = None) -> int:
    return (
        LLMCall.objects.filter(model=model, created_at__gte=quota_day_start(now))
        .exclude(status__in=_NOT_BILLED)
        .count()
    )


def check_daily_quota(model: str, rpd: int, threshold: float, now: datetime | None = None) -> int:
    """Lanza DailyQuotaExceeded si ya se usó el `threshold` (95%) del límite diario. Devuelve las usadas."""
    used = calls_today(model, now)
    if used >= math.floor(rpd * threshold):
        raise DailyQuotaExceeded(model, used=used, limit=rpd)
    return used
