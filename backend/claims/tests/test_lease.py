"""Tests del análisis de contrato (Prompt A) con FakeGeminiClient.

La mayoría usa un prompt y esquema mínimos; test_real_prompt_and_schema usa los del apéndice.

    docker compose exec backend python manage.py test claims.tests.test_lease
"""

from datetime import date
from pathlib import Path

from django.test import TestCase
from pydantic import BaseModel

from claims.llm.fake import FakeGeminiClient
from claims.llm.rate_limit import MinuteRateLimiter
from claims.models import Claim, ClaimDocument, LeaseExtraction, LLMCall
from claims.pipeline.lease import analyze_lease, lease_cache_key

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
TEMPLATE = 'Move-out date: {{move_out_date}}. Reply as {"document_check": {...}}.'


class DocumentCheck(BaseModel):
    is_lease: bool
    detected_type: str | None = None


class FakeLeaseTerms(BaseModel):
    document_check: DocumentCheck
    terms: dict = {}


class AnalyzeLeaseTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(tracking_id="T1", move_out_date=date(2022, 3, 1))

    def make_client(self, scenario):
        no_wait = MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        return FakeGeminiClient(responses_dir=FIXTURES / scenario, limiter=no_wait, sleep=lambda s: None)

    def add(self, name, doc_type="lease", confidence=""):
        return ClaimDocument.objects.create(
            claim=self.claim, file_path=f"claims/T1/{name}", file_name=name, file_hash=f"hash-{name}",
            mime_type="application/pdf", doc_type=doc_type, classification_confidence=confidence,
        )

    def analyze(self, client):
        return analyze_lease(self.claim, client=client, template=TEMPLATE, schema=FakeLeaseTerms)

    def test_no_lease_documents_returns_none_without_calling(self):
        self.add("ledger.pdf", doc_type="ledger")
        client = self.make_client("lease_valid")

        self.assertIsNone(self.analyze(client))
        self.assertEqual(client.calls, [])

    def test_valid_lease_is_saved(self):
        lease = self.add("Lease.pdf", confidence="high")
        addendum = self.add("Addendum.pdf", doc_type="lease_mixed", confidence="high")
        client = self.make_client("lease_valid")

        extraction = self.analyze(client)

        self.assertTrue(extraction.is_lease)
        self.assertEqual(extraction.raw_json["terms"], {"pet_fee": "non-refundable"})
        self.assertEqual((extraction.prompt_version, extraction.model_used), ("lease-v1", "gemini-3.5-flash-lite"))
        self.assertEqual(set(extraction.documents.all()), {lease, addendum})
        self.assertEqual(len(client.calls), 1)  # todos juntos en una sola llamada
        self.assertEqual(len(client.calls[0]["files"]), 2)
        self.assertIn("Move-out date: 2022-03-01.", client.calls[0]["prompt"])
        self.assertEqual(LLMCall.objects.get().purpose, "lease")

    def test_not_a_lease_reclassifies_and_returns_none(self):
        doc = self.add("Lease.pdf", confidence="high")
        client = self.make_client("lease_not_lease")

        self.assertIsNone(self.analyze(client))

        doc.refresh_from_db()
        self.assertEqual((doc.doc_type, doc.classification_method), ("ledger", "prompt_a_guard"))
        extraction = LeaseExtraction.objects.get(claim=self.claim)
        self.assertFalse(extraction.is_lease)

    def test_not_a_lease_is_not_called_again(self):
        self.add("Lease.pdf")
        client = self.make_client("lease_not_lease")
        self.analyze(client)

        self.assertIsNone(self.analyze(client))
        self.assertEqual(len(client.calls), 1)

    def test_second_call_uses_cache(self):
        self.add("Lease.pdf")
        client = self.make_client("lease_valid")
        first = self.analyze(client)

        second = self.analyze(client)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(LLMCall.objects.count(), 1)

    def test_new_document_invalidates_cache(self):
        self.add("Lease.pdf")
        client = self.make_client("lease_valid")
        first = self.analyze(client)
        self.add("Renewal.pdf")

        second = self.analyze(client)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(second.pk, first.pk)  # una sola extracción por claim, actualizada
        self.assertEqual(second.documents.count(), 2)

    def test_medium_confidence_sends_one_by_one(self):
        invoice = self.add("scan1.pdf", confidence="medium")
        lease = self.add("scan2.pdf", confidence="high")
        client = self.make_client("lease_medium")  # 1a respuesta: no es lease; 2a: sí

        extraction = self.analyze(client)

        self.assertEqual([len(c["files"]) for c in client.calls], [1, 1])
        invoice.refresh_from_db()
        self.assertEqual((invoice.doc_type, invoice.classification_method), ("invoice", "prompt_a_guard"))
        self.assertTrue(extraction.is_lease)
        self.assertEqual(list(extraction.documents.all()), [lease])

        # La siguiente vez solo queda el contrato confirmado y se usa la caché
        self.assertEqual(self.analyze(client).pk, extraction.pk)
        self.assertEqual(len(client.calls), 2)

    def test_cache_key_ignores_document_order(self):
        a, b = self.add("a.pdf"), self.add("b.pdf")
        self.assertEqual(lease_cache_key([a, b], "m"), lease_cache_key([b, a], "m"))
        self.assertNotEqual(lease_cache_key([a, b], "m"), lease_cache_key([a, b], "other-model"))

    def test_real_prompt_and_schema(self):
        """Sin template ni schema: usa claims/prompts/lease.md y schemas.LeaseTerms del apéndice."""
        self.add("Lease.pdf")
        client = self.make_client("pipeline_done")  # respuesta válida para LeaseTerms

        extraction = analyze_lease(self.claim, client=client)

        prompt = client.calls[0]["prompt"]
        self.assertTrue(prompt.startswith("You are a legal document analyst"))
        self.assertIn("Move-out date for this claim: 2022-03-01", prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual(extraction.raw_json["program"]["coverage_amount"], 2500.0)

    def test_missing_move_out_date_is_not_provided(self):
        self.claim.move_out_date = None
        self.claim.save()
        self.add("Lease.pdf")
        client = self.make_client("lease_valid")
        self.analyze(client)
        self.assertIn("Move-out date: Not provided.", client.calls[0]["prompt"])
