"""Tests de la cola (RQ) con FakeGeminiClient.

Usan el Redis de docker compose, en la base 15 (la cola real usa la 0), y un SimpleWorker
en el mismo proceso, así ven los datos de la base de prueba.

    docker compose exec backend python manage.py test claims.tests.test_jobs
"""

from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

import redis
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from rq import Queue, SimpleWorker

from claims.jobs import enqueue_claim, job_id_for, queue_position
from claims.llm.rate_limit import MinuteRateLimiter
from claims.models import Claim, ClaimDocument, LLMCall
from claims.tests.test_classify import LEASE_TEXT, LEDGER_TEXT

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
TEST_REDIS_URL = "redis://redis:6379/15"
NO_WAIT = MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None)


@override_settings(GEMINI_FAKE=True, GEMINI_FAKE_RESPONSES_DIR=str(FIXTURES / "pipeline_done"))
@mock.patch("claims.llm.client.minute_limiter", NO_WAIT)
class QueueTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.redis = redis.Redis.from_url(TEST_REDIS_URL)
        try:
            cls.redis.ping()
        except redis.ConnectionError:
            cls.skipTest(cls, "Redis no está disponible (docker compose up redis)")

    def setUp(self):
        self.redis.flushdb()
        self.queue = Queue("default", connection=self.redis)

    def tearDown(self):
        self.redis.flushdb()

    def make_claim(self, tracking):
        claim = Claim.objects.create(tracking_id=tracking, max_benefit=Decimal("2500.00"),
                                     claim_amount=Decimal("1775.00"))
        for name, text, pages in (("Lease.pdf", LEASE_TEXT, 8), ("ledger.pdf", LEDGER_TEXT, 1)):
            ClaimDocument.objects.create(
                claim=claim, file_path=f"claims/{tracking}/{name}", file_name=name,
                file_hash=f"{tracking}-{name}", mime_type="application/pdf",
                first_pages_text=text, full_text=text, page_count=pages,
            )
        return claim

    def work(self):
        SimpleWorker([self.queue], connection=self.redis).work(burst=True)

    def status(self, tracking):
        return Claim.objects.get(pk=tracking).processing_status

    def test_three_claims_are_processed_in_order(self):
        claims = [self.make_claim(t) for t in ("30", "10", "20")]
        for claim in claims:
            self.assertEqual(enqueue_claim(claim, queue=self.queue), "queued")
        self.assertEqual([queue_position(t, queue=self.queue) for t in ("30", "10", "20")], [1, 2, 3])
        self.assertEqual(self.status("30"), "queued")

        self.work()

        self.assertEqual([self.status(t) for t in ("30", "10", "20")], ["done", "done", "done"])
        order = list(dict.fromkeys(LLMCall.objects.order_by("id").values_list("claim_id", flat=True)))
        self.assertEqual(order, ["30", "10", "20"])
        self.assertIsNone(queue_position("30", queue=self.queue))

    def test_same_claim_twice_is_not_duplicated(self):
        claim = self.make_claim("1")

        first = enqueue_claim(claim, queue=self.queue)
        second = enqueue_claim(claim, queue=self.queue)

        self.assertEqual((first, second), ("queued", "queued"))
        self.assertEqual(self.queue.count, 1)
        self.assertEqual(self.queue.job_ids, [job_id_for("1")])

    def test_finished_claim_can_be_enqueued_again(self):
        claim = self.make_claim("1")
        enqueue_claim(claim, queue=self.queue)
        self.work()

        self.assertEqual(enqueue_claim(claim, queue=self.queue), "queued")
        self.assertEqual(self.queue.count, 1)

    def test_queued_jobs_stop_without_calling_gemini_once_quota_is_exhausted(self):
        model = f"fake/{settings.GEMINI_MODEL_EXTRACT}"
        # 474 usadas: el claim A hace 1 llamada (contrato) y el 2do paso choca con el 95% (475/500)
        LLMCall.objects.bulk_create(LLMCall(model=model, purpose="lease", status="ok") for _ in range(474))
        a, b = self.make_claim("A"), self.make_claim("B")
        enqueue_claim(a, queue=self.queue)
        enqueue_claim(b, queue=self.queue)

        self.work()

        self.assertEqual(self.status("A"), "quota_exhausted")
        self.assertEqual(self.queue.failed_job_registry.get_job_ids(), [job_id_for("A")])  # A relanzó el error
        b = Claim.objects.get(pk="B")
        self.assertEqual(b.processing_status, "quota_exhausted")
        self.assertIn(settings.GEMINI_MODEL_EXTRACT, b.failure_reason)
        self.assertFalse(LLMCall.objects.filter(claim_id="B").exists())  # B nunca llamó a Gemini

    def test_process_claims_command_enqueues_by_default(self):
        self.make_claim("1")
        self.make_claim("2")
        out = StringIO()

        with mock.patch("claims.jobs.get_queue", return_value=self.queue):
            call_command("process_claims", "--ids", "2,1", stdout=out)

        self.assertEqual(self.queue.job_ids, [job_id_for("2"), job_id_for("1")])
        self.assertIn("Claim 2: queued (posición 1)", out.getvalue())


class DashboardTests(TestCase):
    def test_django_rq_dashboard_shows_default_queue(self):
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)

        response = self.client.get("/django-rq/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "default")

    def test_dashboard_requires_staff(self):
        response = self.client.get("/django-rq/")
        self.assertEqual(response.status_code, 302)
