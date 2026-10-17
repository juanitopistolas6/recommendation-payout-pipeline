"""Tests del orquestador con FakeGeminiClient, de punta a punta.

Usan los prompts (claims/prompts/*.md) y esquemas (claims/llm/schemas.py) reales del apéndice:
las respuestas fijas de fixtures/llm/pipeline_*/ validan contra ellos.

    docker compose exec backend python manage.py test claims.tests.test_orchestrator
"""

from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase

from claims.llm.errors import DailyQuotaExceeded
from claims.llm.fake import FakeGeminiClient
from claims.llm.rate_limit import MinuteRateLimiter
from claims.models import Claim, ClaimDocument, LLMCall, Recommendation
from claims.pipeline.orchestrator import NO_DOCUMENTS, process_claim
from claims.tests.test_classify import LEASE_TEXT, LEDGER_TEXT

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


class ProcessClaimTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(tracking_id="7", max_benefit=Decimal("2500.00"),
                                          claim_amount=Decimal("1775.00"))
        self.lease_doc = self.add("Bell Lease.pdf", LEASE_TEXT, pages=8)
        self.ledger_doc = self.add("ledger.pdf", LEDGER_TEXT)

    def add(self, name, text, pages=1):
        return ClaimDocument.objects.create(
            claim=self.claim, file_path=f"claims/7/{name}", file_name=name, file_hash=f"hash-{name}",
            mime_type="application/pdf", first_pages_text=text, full_text=text, page_count=pages,
        )

    def make_client(self, scenario):
        no_wait = MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        return FakeGeminiClient(responses_dir=FIXTURES / scenario, limiter=no_wait, sleep=lambda s: None)

    def status(self):
        self.claim.refresh_from_db()
        return self.claim.processing_status, self.claim.failure_reason

    def test_claim_reaches_done(self):
        client = self.make_client("pipeline_done")

        process_claim("7", client=client)

        self.assertEqual(self.status(), ("done", ""))
        rec = Recommendation.objects.get(claim=self.claim)
        self.assertEqual((rec.recommended_amount, rec.route), (Decimal("1775.00"), "auto"))
        self.assertEqual([c["purpose"] for c in client.calls], ["lease", "claim"])
        self.lease_doc.refresh_from_db()
        self.assertEqual(self.lease_doc.doc_type, "lease")

    def test_quota_exhausted(self):
        model = f"fake/{settings.GEMINI_MODEL_EXTRACT}"
        LLMCall.objects.bulk_create(LLMCall(model=model, purpose="lease", status="ok") for _ in range(475))

        with self.assertRaises(DailyQuotaExceeded):  # se relanza para la cola
            process_claim("7", client=self.make_client("pipeline_done"))

        status, reason = self.status()
        self.assertEqual(status, "quota_exhausted")
        self.assertIn(settings.GEMINI_MODEL_EXTRACT, reason)
        self.assertIn("475/500", reason)
        self.assertIn("medianoche del Pacífico", reason)

    def test_invalid_json_fails(self):
        process_claim("7", client=self.make_client("pipeline_invalid"))

        status, reason = self.status()
        self.assertEqual(status, "failed")
        self.assertIn("Salida inválida del modelo", reason)
        self.assertFalse(Recommendation.objects.exists())

    def test_claim_without_documents_fails(self):
        Claim.objects.create(tracking_id="8")

        process_claim("8", client=self.make_client("pipeline_done"))

        claim = Claim.objects.get(pk="8")
        self.assertEqual((claim.processing_status, claim.failure_reason), ("failed", NO_DOCUMENTS))

    def test_unexpected_error_fails_with_summarized_traceback(self):
        with mock.patch("claims.pipeline.orchestrator.classify_claim_documents", side_effect=RuntimeError("boom")):
            with self.assertLogs("claims.pipeline.orchestrator", level="ERROR"):
                process_claim("7", client=self.make_client("pipeline_done"))

        status, reason = self.status()
        self.assertEqual(status, "failed")
        self.assertIn("RuntimeError: boom", reason)
        self.assertIn("orchestrator.py", reason)

    def test_retry_with_stronger_model_that_resolves(self):
        client = self.make_client("pipeline_retry_ok")

        process_claim("7", client=client)

        self.assertEqual(self.status(), ("done", ""))
        self.assertEqual([c["purpose"] for c in client.calls], ["lease", "claim", "retry"])
        self.assertEqual(client.calls[-1]["model"], settings.GEMINI_MODEL_RETRY)
        self.assertEqual(Recommendation.objects.get().route, "auto")

    def test_retry_that_does_not_resolve_goes_to_review(self):
        client = self.make_client("pipeline_retry_review")

        process_claim("7", client=client)

        self.assertEqual(self.status(), ("needs_review", ""))
        self.assertEqual([c["purpose"] for c in client.calls], ["lease", "claim", "retry"])  # una sola vez
        rec = Recommendation.objects.get()
        self.assertEqual(rec.route, "review")
        self.assertIn("totals_mismatch", rec.flags)

    def test_second_run_uses_cache(self):
        client = self.make_client("pipeline_done")
        process_claim("7", client=client)

        process_claim("7", client=client)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(Recommendation.objects.count(), 1)

    def test_force_from_step_3_only_repeats_claim_analysis(self):
        client = self.make_client("pipeline_done")
        process_claim("7", client=client)

        process_claim("7", force_from_step=3, client=client)

        self.assertEqual([c["purpose"] for c in client.calls], ["lease", "claim", "claim"])

    def test_force_from_step_1_reclassifies_except_manual(self):
        client = self.make_client("pipeline_done")
        process_claim("7", client=client)
        ClaimDocument.objects.filter(pk=self.ledger_doc.pk).update(doc_type="invoice", classification_method="manual")

        process_claim("7", force_from_step=1, client=client)

        self.ledger_doc.refresh_from_db()
        self.assertEqual((self.ledger_doc.doc_type, self.ledger_doc.classification_method), ("invoice", "manual"))
        self.assertEqual([c["purpose"] for c in client.calls], ["lease", "claim", "lease", "claim"])
        self.assertEqual(self.status(), ("done", ""))


class ProcessClaimsCommandTests(TestCase):
    def test_unknown_ids(self):
        with self.assertRaises(CommandError):
            call_command("process_claims", "--ids", "999", "--sync", stdout=StringIO())

    def test_sync_reports_status(self):
        Claim.objects.create(tracking_id="1")
        out = StringIO()
        call_command("process_claims", "--ids", "1", "--sync", stdout=out)
        self.assertIn(f"failed: {NO_DOCUMENTS}", out.getvalue())
