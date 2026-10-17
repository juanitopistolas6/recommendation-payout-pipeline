"""Tests de la capa LLM con FakeGeminiClient: nunca llaman a la API ni gastan cuota.

    docker compose exec backend python manage.py test claims.tests.test_llm
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from pydantic import BaseModel

from claims.llm.client import render_prompt
from claims.llm.errors import DailyQuotaExceeded, InvalidModelOutput, LLMUnavailable
from claims.llm.fake import FakeGeminiClient
from claims.llm.rate_limit import MinuteRateLimiter, quota_day_start
from claims.models import LLMCall

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


class DocInfo(BaseModel):
    document_type: str
    pages: int


class FakeClock:
    """Reloj controlado: sleep() avanza el tiempo en vez de dormir."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class LLMClientTestBase(TestCase):
    model = settings.GEMINI_MODEL_CLASSIFY
    log_model = f"fake/{settings.GEMINI_MODEL_CLASSIFY}"

    def make_client(self, scenario: str) -> FakeGeminiClient:
        self.clock = FakeClock()
        self.retry_sleeps: list[float] = []
        return FakeGeminiClient(
            responses_dir=FIXTURES / scenario,
            limiter=MinuteRateLimiter(clock=self.clock, sleep=self.clock.sleep),
            sleep=self.retry_sleeps.append,
        )

    def generate(self, client: FakeGeminiClient):
        return client.generate(model=self.model, purpose="classify", prompt="¿Qué documento es?", schema=DocInfo)

    def statuses(self) -> list[str]:
        return list(LLMCall.objects.order_by("id").values_list("status", flat=True))


class ValidationRetryTests(LLMClientTestBase):
    def test_valid_response_is_parsed_and_logged(self):
        result = self.generate(self.make_client("valid"))

        self.assertEqual(result, DocInfo(document_type="lease", pages=3))
        call = LLMCall.objects.get()
        self.assertEqual((call.model, call.purpose, call.status), (self.log_model, "classify", "ok"))
        self.assertIsNotNone(call.latency_ms)

    def test_invalid_json_is_retried_once(self):
        client = self.make_client("invalid_then_valid")

        result = self.generate(client)

        self.assertEqual(result.document_type, "lease")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.statuses(), ["invalid_output", "ok"])

    def test_invalid_twice_raises_invalid_model_output(self):
        client = self.make_client("always_invalid")

        with self.assertRaises(InvalidModelOutput) as ctx:
            self.generate(client)

        self.assertEqual(len(client.calls), 2)  # 1 intento + 1 reintento, no más
        self.assertEqual(self.statuses(), ["invalid_output", "invalid_output"])
        self.assertIn("tres", ctx.exception.raw_text)


class DailyQuotaTests(LLMClientTestBase):
    rpd = settings.GEMINI_RATE_LIMITS[settings.GEMINI_MODEL_CLASSIFY]["rpd"]  # 500 -> corta en 475

    def log_calls(self, n: int, status: str = "ok") -> None:
        LLMCall.objects.bulk_create(
            LLMCall(model=self.log_model, purpose="classify", status=status) for _ in range(n)
        )

    def test_raises_at_95_percent_without_calling_api(self):
        self.log_calls(int(self.rpd * 0.95))
        client = self.make_client("valid")

        with self.assertRaises(DailyQuotaExceeded) as ctx:
            self.generate(client)

        self.assertEqual(client.calls, [])  # nunca llegó a la API
        self.assertEqual(ctx.exception.used, 475)
        self.assertEqual(LLMCall.objects.count(), 475)  # no registra una llamada que no se hizo

    def test_allows_call_just_below_threshold(self):
        self.log_calls(int(self.rpd * 0.95) - 1)
        self.generate(self.make_client("valid"))

    def test_rate_limited_calls_do_not_count(self):
        self.log_calls(int(self.rpd * 0.95), status="rate_limited_minute")
        self.generate(self.make_client("valid"))

    def test_calls_before_pacific_midnight_do_not_count(self):
        self.log_calls(int(self.rpd * 0.95))
        LLMCall.objects.update(created_at=quota_day_start() - timedelta(minutes=1))
        self.generate(self.make_client("valid"))

    def test_real_model_quota_is_separate_from_fake(self):
        LLMCall.objects.bulk_create(
            LLMCall(model=self.model, purpose="classify", status="ok") for _ in range(self.rpd)
        )
        self.generate(self.make_client("valid"))  # las llamadas reales no frenan al fake

    def test_api_daily_429_raises_immediately_without_retry(self):
        client = self.make_client("daily_429")

        with self.assertRaises(DailyQuotaExceeded):
            self.generate(client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.retry_sleeps, [])
        self.assertEqual(self.statuses(), ["rate_limited_day"])


class QuotaDayStartTests(SimpleTestCase):
    def test_day_starts_at_pacific_midnight(self):
        # 06:30 UTC del 17 de octubre = 23:30 del 16 en Los Ángeles (PDT, UTC-7)
        now = datetime(2026, 10, 17, 6, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(quota_day_start(now), datetime(2026, 10, 16, 7, 0, tzinfo=dt_timezone.utc))

    def test_day_start_in_winter_time(self):
        # 09:00 UTC del 15 de enero = 01:00 en Los Ángeles (PST, UTC-8)
        now = datetime(2027, 1, 15, 9, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(quota_day_start(now), datetime(2027, 1, 15, 8, 0, tzinfo=dt_timezone.utc))


class MinuteIntervalTests(LLMClientTestBase):
    interval = 60 / 15 * 1.1  # 15 RPM + 10% = 4.4 s

    def test_consecutive_calls_wait_the_minimum_interval(self):
        client = self.make_client("valid")

        self.generate(client)
        self.generate(client)
        self.clock.now += 1.0  # pasa 1 s antes de la tercera
        self.generate(client)

        self.assertEqual(len(self.clock.sleeps), 2)
        self.assertAlmostEqual(self.clock.sleeps[0], self.interval)
        self.assertAlmostEqual(self.clock.sleeps[1], self.interval - 1.0)

    def test_no_wait_after_interval_elapsed(self):
        client = self.make_client("valid")

        self.generate(client)
        self.clock.now += self.interval + 0.1
        self.generate(client)

        self.assertEqual(self.clock.sleeps, [])

    def test_models_are_limited_independently(self):
        clock = FakeClock()
        limiter = MinuteRateLimiter(clock=clock, sleep=clock.sleep)

        limiter.wait("a", rpm=15, margin=0.1)
        limiter.wait("b", rpm=5, margin=0.1)

        self.assertEqual(clock.sleeps, [])

    def test_slower_model_has_longer_interval(self):
        self.assertAlmostEqual(MinuteRateLimiter.interval(rpm=5, margin=0.1), 13.2)


class TransientErrorRetryTests(LLMClientTestBase):
    def test_minute_429_is_retried_with_exponential_backoff(self):
        client = self.make_client("minute_429_then_ok")

        result = self.generate(client)

        self.assertEqual(result.pages, 3)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(self.retry_sleeps, [10, 20])  # exponencial desde 10 s
        self.assertEqual(self.statuses(), ["rate_limited_minute", "rate_limited_minute", "ok"])

    def test_server_error_gives_up_after_3_attempts(self):
        client = self.make_client("server_error")

        with self.assertRaises(LLMUnavailable):
            self.generate(client)

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(self.statuses(), ["error", "error", "error"])


class RenderPromptTests(SimpleTestCase):
    def test_replaces_placeholders_and_keeps_json_braces(self):
        template = 'Dirección: {{address}}\nResponde: {"total": {"monto": 0}} y {{ address }}'

        result = render_prompt(template, address="305 Coosawatchie St")

        self.assertEqual(result, 'Dirección: 305 Coosawatchie St\nResponde: {"total": {"monto": 0}} y 305 Coosawatchie St')

    def test_values_with_braces_are_inserted_literally(self):
        self.assertEqual(render_prompt("{{x}}", x="{y}"), "{y}")

    def test_missing_variable_raises(self):
        with self.assertRaises(ValueError):
            render_prompt("{{address}} {{rent}}", address="x")

    def test_unknown_variable_raises(self):
        with self.assertRaises(ValueError):
            render_prompt("{{address}}", address="x", typo="y")
