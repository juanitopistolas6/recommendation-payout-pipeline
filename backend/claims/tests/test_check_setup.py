"""Tests de check_setup: cada verificación sin Gemini ni Redis reales (la cola se simula).

    docker compose exec backend python manage.py test claims.tests.test_check_setup
"""

import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from claims.models import Claim, ClaimDocument, LLMCall

CLASSIFY, EXTRACT, RETRY = "model-classify", "model-extract", "model-retry"
LIMITS = {CLASSIFY: {"rpm": 15, "rpd": 100}, EXTRACT: {"rpm": 15, "rpd": 100}, RETRY: {"rpm": 5, "rpd": 20}}
COMMAND = "claims.management.commands.check_setup"


class CheckSetupTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        settings_patch = override_settings(
            DATA_ROOT=str(self.root), GEMINI_FAKE=False, GEMINI_API_KEY="real-key-1234",
            GEMINI_MODEL_CLASSIFY=CLASSIFY, GEMINI_MODEL_EXTRACT=EXTRACT, GEMINI_MODEL_RETRY=RETRY,
            GEMINI_RATE_LIMITS=LIMITS,
        )
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)

        self.rq = mock.patch(f"{COMMAND}.django_rq").start()
        self.rq.get_queue.return_value.count = 0
        self.workers = mock.patch(f"{COMMAND}.Worker").start()
        self.workers.all.return_value = [mock.Mock()]
        self.addCleanup(mock.patch.stopall)

        self.claim = Claim.objects.create(tracking_id="1", max_benefit=Decimal("2500.00"))
        self.add_document("lease.pdf")

    def add_document(self, name, create_file=True, claim=None):
        claim = claim or self.claim
        relative = f"claims/{claim.pk}/{name}"
        if create_file:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4")
        return ClaimDocument.objects.create(claim=claim, file_path=relative, file_name=name, file_hash=relative,
                                            mime_type="application/pdf")

    def run_check(self, *args):
        out = StringIO()
        try:
            call_command("check_setup", *args, stdout=out)
        except CommandError as exc:
            return out.getvalue(), str(exc)
        return out.getvalue(), None

    def use_quota(self, model, count):
        LLMCall.objects.bulk_create(
            LLMCall(model=model, purpose="claim", status=LLMCall.Status.OK) for _ in range(count)
        )

    def test_everything_ready(self):
        output, error = self.run_check()

        self.assertIsNone(error)
        self.assertIn("Todo listo", output)
        self.assertIn("(...1234)", output)
        self.assertIn("Los 1 archivos que se mandan a Gemini existen", output)
        self.assertNotIn("[ERROR]", output)

    def test_missing_api_key_fails(self):
        with override_settings(GEMINI_API_KEY="your-gemini-api-key"):
            output, error = self.run_check()

        self.assertIn("[ERROR] Falta GEMINI_API_KEY", output)
        self.assertIn("1 error(es)", error)

    def test_fake_mode_is_only_a_warning(self):
        with override_settings(GEMINI_FAKE=True, GEMINI_API_KEY=""):
            output, error = self.run_check()

        self.assertIsNone(error)
        self.assertIn("[AVISO] GEMINI_FAKE=1", output)
        self.assertIn("[AVISO] Falta GEMINI_API_KEY", output)

    def test_model_without_limits_fails(self):
        with override_settings(GEMINI_MODEL_EXTRACT="unknown-model"):
            output, error = self.run_check()

        self.assertIn("extract: unknown-model no tiene límites", output)
        self.assertIsNotNone(error)

    def test_exhausted_extract_quota_fails_but_retry_only_warns(self):
        self.use_quota(EXTRACT, 95)
        self.use_quota(RETRY, 19)

        output, error = self.run_check()

        self.assertIn("[ERROR] extract: model-extract — usadas hoy 95/100 (corte en 95)", output)
        self.assertIn("[AVISO] retry: model-retry — usadas hoy 19/20", output)
        self.assertIn("1 error(es)", error)

    def test_rate_limited_calls_do_not_count(self):
        LLMCall.objects.create(model=EXTRACT, purpose="claim", status=LLMCall.Status.RATE_LIMITED_DAY)

        output, _ = self.run_check()

        self.assertIn("usadas hoy 0/100", output)

    def test_no_claims_fails(self):
        Claim.objects.all().delete()

        output, error = self.run_check()

        self.assertIn("[ERROR] No hay claims", output)
        self.assertIsNotNone(error)

    def test_claims_without_documents_warn(self):
        Claim.objects.create(tracking_id="2", max_benefit=Decimal("1000.00"))

        output, error = self.run_check()

        self.assertIsNone(error)
        self.assertIn("[AVISO] 1 claims sin documentos", output)

    def test_missing_files_are_listed(self):
        self.add_document("ledger.pdf", create_file=False)

        output, error = self.run_check()

        self.assertIn("Faltan 1 de 2 archivos originales, ej.: claims/1/ledger.pdf", output)
        self.assertIsNotNone(error)

    def test_missing_converted_file_is_reported_apart(self):
        doc = self.add_document("notes.docx")
        doc.converted_pdf_path = "_converted/1/notes.docx.pdf"
        doc.save()

        output, error = self.run_check()

        self.assertIn("Faltan 1 archivos convertidos", output)
        self.assertIn("_converted/1/notes.docx.pdf", output)
        self.assertIsNotNone(error)

    def test_ids_limit_the_file_check(self):
        other = Claim.objects.create(tracking_id="2", max_benefit=Decimal("1000.00"))
        self.add_document("lost.pdf", create_file=False, claim=other)

        output, error = self.run_check("--ids", "1")

        self.assertIsNone(error)
        self.assertIn("1 claims", output)

    def test_unknown_ids_fail(self):
        output, error = self.run_check("--ids", "1,999")

        self.assertIn("No existen los claims: 999", output)
        self.assertIsNotNone(error)

    def test_missing_claims_folder_fails(self):
        shutil.rmtree(self.root / "claims")

        output, error = self.run_check()

        self.assertIn(f"[ERROR] No existe {self.root / 'claims'}", output)
        self.assertIsNotNone(error)

    def test_redis_down_fails(self):
        self.rq.get_connection.return_value.ping.side_effect = ConnectionError("refused")

        output, error = self.run_check()

        self.assertIn("[ERROR] Redis no responde: refused", output)
        self.assertIsNotNone(error)

    def test_no_worker_fails(self):
        self.workers.all.return_value = []

        output, error = self.run_check()

        self.assertIn("No hay worker escuchando la cola 'default'", output)
        self.assertIsNotNone(error)

    def test_live_calls_smoke_test_with_an_existing_document(self):
        with mock.patch(f"{COMMAND}.call_command") as smoke:
            output, error = self.run_check("--live")

        self.assertIsNone(error)
        self.assertEqual(smoke.call_args.args, ("llm_smoke_test", str(self.root / "claims/1/lease.pdf")))
        self.assertIn("Gemini respondió", output)

    def test_live_failure_fails(self):
        with mock.patch(f"{COMMAND}.call_command", side_effect=CommandError("LLMRequestError: 403")):
            output, error = self.run_check("--live")

        self.assertIn("[ERROR] La llamada falló: LLMRequestError: 403", output)
        self.assertIsNotNone(error)

    def test_live_is_skipped_when_something_else_fails(self):
        self.workers.all.return_value = []

        with mock.patch(f"{COMMAND}.call_command") as smoke:
            output, _ = self.run_check("--live")

        smoke.assert_not_called()
        self.assertIn("Se omite: primero corrige los errores", output)
