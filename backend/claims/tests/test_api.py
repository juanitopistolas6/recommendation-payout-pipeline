"""Tests de la API (ninja) y del streaming SSE.

    docker compose exec backend python manage.py test claims.tests.test_api
"""

import json
from datetime import date
from decimal import Decimal
from unittest import mock

import redis
from asgiref.sync import async_to_sync, sync_to_async
from django.test import AsyncClient, TestCase
from rq import Queue

from claims.jobs import job_id_for
from claims.models import Claim, ClaimDocument, ClaimExtraction, ClaimOutcome, Recommendation
from claims.sse import _events, get_states

TEST_REDIS_URL = "redis://redis:6379/15"


class QueueMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.redis = redis.Redis.from_url(TEST_REDIS_URL)
        try:
            cls.redis.ping()
        except redis.ConnectionError:
            cls.skipTest(cls, "Redis no está disponible")

    def setUp(self):
        self.redis.flushdb()
        self.queue = Queue("default", connection=self.redis)
        patcher = mock.patch("claims.jobs.get_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.redis.flushdb)


def make_claim(tracking, status="pending", **fields):
    return Claim.objects.create(tracking_id=tracking, processing_status=status, **fields)


def make_recommendation(claim, amount="2500.00", route="auto"):
    extraction = ClaimExtraction.objects.create(
        claim=claim, prompt_version="claim-v1", model_used="gemini-3.5-flash-lite", cache_key="k", confidence="high",
        raw_json={"payments_and_credits": [{"type": "payment", "amount": "100.00"}], "findings": ["carpet stains"]},
    )
    return Recommendation.objects.create(
        claim=claim, extraction=extraction, recommended_amount=Decimal(amount), eligible_owed=Decimal("7063.23"),
        excluded_total=Decimal("2225.96"), cap_applied=True, flags=["claim_amount_mismatch"], route=route,
        rules_version="v1", explanation="Documented charges: $9,289.19.",
        reasoning=[{"description": "Cleaning", "eligible": True, "reason": "lease 12", "balance": "7063.23"}],
    )


class AnalyzeTests(QueueMixin, TestCase):
    def post(self, ids, force=False):
        return self.client.post("/api/claims/analyze", {"tracking_numbers": ids, "force": force},
                                content_type="application/json")

    def test_enqueues_and_returns_202(self):
        make_claim("1")
        make_claim("2")

        response = self.post(["1", "2"])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), [
            {"tracking_number": "1", "status": "queued", "queue_position": 1, "summary": None, "failure_reason": None},
            {"tracking_number": "2", "status": "queued", "queue_position": 2, "summary": None, "failure_reason": None},
        ])
        self.assertEqual(Claim.objects.get(pk="1").processing_status, "queued")

    def test_normalizes_spaces_and_duplicates(self):
        make_claim("1045")
        response = self.post([" 1045 ", "1045", "", "  "])
        self.assertEqual([r["tracking_number"] for r in response.json()], ["1045"])

    def test_not_found(self):
        self.assertEqual(self.post(["nope"]).json()[0]["status"], "not_found")

    def test_max_50_ids(self):
        self.assertEqual(self.post([str(i) for i in range(51)]).status_code, 422)
        self.assertEqual(self.post(["1"] * 60).status_code, 202)  # 60 repetidos = 1 ID

    def test_with_recommendation_returns_result_without_enqueuing(self):
        make_recommendation(make_claim("1", status="done"))

        item = self.post(["1"]).json()[0]

        self.assertEqual(item["status"], "done")
        self.assertEqual(item["summary"], {"recommended_amount": "2500.00", "route": "auto",
                                           "flags": ["claim_amount_mismatch"], "cap_applied": True})
        self.assertEqual(self.queue.count, 0)

    def test_force_reprocesses(self):
        make_recommendation(make_claim("1", status="done"))

        item = self.post(["1"], force=True).json()[0]

        self.assertEqual((item["status"], item["queue_position"]), ("queued", 1))
        self.assertEqual(self.queue.job_ids, [job_id_for("1")])

    def test_already_queued_reports_position_without_duplicating(self):
        make_claim("1")
        make_claim("2")
        self.post(["1", "2"])

        item = self.post(["2"]).json()[0]

        self.assertEqual((item["status"], item["queue_position"]), ("queued", 2))
        self.assertEqual(self.queue.count, 2)

    def test_failed_is_not_retried_without_force(self):
        make_claim("1", status="failed", failure_reason="sin documentos registrados")

        item = self.post(["1"]).json()[0]

        self.assertEqual((item["status"], item["failure_reason"]), ("failed", "sin documentos registrados"))
        self.assertEqual(self.queue.count, 0)
        self.assertEqual(self.post(["1"], force=True).json()[0]["status"], "queued")

    def test_quota_exhausted_is_enqueued_again(self):
        make_claim("1", status="quota_exhausted")
        self.assertEqual(self.post(["1"]).json()[0]["status"], "queued")


class ClaimDetailTests(QueueMixin, TestCase):
    def test_detail(self):
        claim = make_claim("1", status="done", monthly_rent=Decimal("1450.00"), max_benefit=Decimal("2500.00"),
                           move_out_date=date(2022, 3, 1), lease_street_address="305 Coosawatchie St")
        ClaimDocument.objects.create(claim=claim, file_path="claims/x/l.pdf", file_name="ledger.pdf", file_hash="h",
                                     doc_type="ledger", classification_method="rules", page_count=2)
        make_recommendation(claim)

        data = self.client.get("/api/claims/1").json()

        self.assertEqual(data["claim"]["tracking_number"], "1")
        self.assertEqual(data["claim"]["lease_street_address"], "305 Coosawatchie St")
        self.assertEqual(data["claim"]["move_out_date"], "2022-03-01")
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["documents"][0]["file_name"], "ledger.pdf")
        rec = data["recommendation"]
        self.assertEqual((rec["route"], rec["flags"], rec["cap_applied"]), ("auto", ["claim_amount_mismatch"], True))
        self.assertEqual(rec["charges"][0]["reason"], "lease 12")
        self.assertEqual(rec["payments"], [{"type": "payment", "amount": "100.00"}])
        self.assertEqual(rec["findings"], ["carpet stains"])

    def test_without_recommendation(self):
        make_claim("1")
        self.assertIsNone(self.client.get("/api/claims/1").json()["recommendation"])

    def test_not_found(self):
        self.assertEqual(self.client.get("/api/claims/nope").status_code, 404)

    def test_no_claim_outcome_data(self):
        claim = make_claim("1", status="done")
        make_recommendation(claim)
        ClaimOutcome.objects.create(claim=claim, approved_benefit=Decimal("8765.43"), status="OUTCOME-STATUS-X",
                                    hold_reason="OUTCOME-HOLD-X", raw_outcome={"Status": "OUTCOME-RAW-X"})

        body = self.client.get("/api/claims/1").content.decode()

        for secret in ("8765.43", "OUTCOME-STATUS-X", "OUTCOME-HOLD-X", "OUTCOME-RAW-X", "approved_benefit"):
            self.assertNotIn(secret, body)


def collect(ids, on_snapshot=None, **kwargs):
    """Consume el generador SSE. on_snapshot se ejecuta (en la BD) después del primer keep-alive."""

    async def run():
        events = []
        async for chunk in _events(ids, **kwargs):
            events.append(chunk)
            if chunk.startswith(": keep-alive") and on_snapshot is not None:
                await sync_to_async(on_snapshot)()
        return events

    return async_to_sync(run)()


def parse(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith(":"):
            out.append(("keep-alive", None))
            continue
        name = chunk.split("\n")[0].removeprefix("event: ")
        data = json.loads(chunk.split("\n")[1].removeprefix("data: "))
        out.append((name, data.get("tracking_number") and (data["tracking_number"], data["status"])))
    return out


class StreamTests(QueueMixin, TestCase):
    def test_get_states(self):
        make_recommendation(make_claim("1", status="done"))
        states = get_states(["1", "nope"])
        self.assertEqual(states["1"]["summary"]["recommended_amount"], "2500.00")
        self.assertIsNone(states["1"]["queue_position"])
        self.assertEqual(states["nope"]["status"], "not_found")

    def test_get_states_queue_position(self):
        make_claim("1")
        make_claim("2")
        self.client.post("/api/claims/analyze", {"tracking_numbers": ["1", "2"]}, content_type="application/json")

        states = get_states(["1", "2"])

        self.assertEqual((states["1"]["queue_position"], states["2"]["queue_position"]), (1, 2))

    def test_queue_position_change_is_sent(self):
        make_claim("1")
        make_claim("2")
        self.client.post("/api/claims/analyze", {"tracking_numbers": ["1", "2"]}, content_type="application/json")
        calls = []

        def first_job_leaves_queue():
            if not calls:
                calls.append(1)
                self.queue.remove(job_id_for("1"))  # el claim 2 no cambia en la BD, solo avanza en la cola

        chunks = collect(["2"], on_snapshot=first_job_leaves_queue, poll_seconds=0, max_seconds=0.05)
        positions = [json.loads(c.split("\n")[1].removeprefix("data: "))["queue_position"]
                     for c in chunks if c.startswith("event: claim")]

        self.assertEqual(positions[:2], [2, 1])

    def test_snapshot_then_complete_when_all_final(self):
        make_claim("1", status="done")
        self.assertEqual(parse(collect(["1", "nope"], poll_seconds=0)), [
            ("claim", ("1", "done")), ("claim", ("nope", "not_found")), ("complete", None),
        ])

    def test_sends_only_changes_then_complete(self):
        make_claim("1", status="queued")
        make_claim("2", status="done")
        calls = []

        def worker_finishes():
            if not calls:  # solo la primera vez
                calls.append(1)
                claim = Claim.objects.get(pk="1")
                claim.processing_status = "needs_review"
                claim.save()

        events = parse(collect(["1", "2"], on_snapshot=worker_finishes, poll_seconds=0))

        self.assertEqual(events, [
            ("claim", ("1", "queued")), ("claim", ("2", "done")),   # snapshot
            ("keep-alive", None),
            ("claim", ("1", "needs_review")),                      # solo el que cambió
            ("complete", None),
        ])

    def test_timeout(self):
        make_claim("1", status="queued")
        events = parse(collect(["1"], poll_seconds=0, max_seconds=0.05))
        self.assertEqual(events[0], ("claim", ("1", "queued")))
        self.assertEqual(events[-1], ("timeout", None))
        self.assertIn(("keep-alive", None), events)


class StreamViewTests(TestCase):
    def test_headers_and_body(self):
        make_claim("1", status="done")

        async def run():
            response = await AsyncClient().get("/api/claims/stream", {"ids": "1, 1"})
            body = b"".join([chunk async for chunk in response.streaming_content])
            return response, body.decode()

        response, body = async_to_sync(run)()

        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertEqual(body.count("event: claim"), 1)  # IDs normalizados
        self.assertTrue(body.endswith("event: complete\ndata: {\"ids\": [\"1\"]}\n\n"))

    def test_requires_ids(self):
        async def run():
            return await AsyncClient().get("/api/claims/stream")

        self.assertEqual(async_to_sync(run)().status_code, 400)
