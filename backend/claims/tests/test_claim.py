"""Tests del análisis del claim (Prompt B) con FakeGeminiClient.

Usan el esquema real (schemas.ClaimExtraction). La mayoría usa una plantilla corta para verificar
línea por línea; test_real_prompt_* usa claims/prompts/claim.md tal cual.

    docker compose exec backend python manage.py test claims.tests.test_claim
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import TestCase
from claims.llm.fake import FakeGeminiClient
from claims.llm.schemas import ClaimExtraction as ClaimExtractionSchema
from claims.llm.rate_limit import MinuteRateLimiter
from claims.models import Claim, ClaimDocument, ClaimExtraction, ClaimOutcome, LeaseExtraction
from claims.pipeline.claim import NO_LEASE, analyze_claim

FIXTURES = Path(__file__).parent / "fixtures" / "llm"

TEMPLATE = """Claim {{tracking_number}}
Rent: {{monthly_rent}} | Lease: {{lease_start_date}} -> {{lease_end_date}} | Move-out: {{move_out_date}}
Termination: {{termination_type}} | Claimed: {{claim_amount}}
PM says: {{pm_explanation}}
Documents:
{{document_list}}
Lease terms:
{{lease_terms_json}}
Reply as {"line_items": [...]}"""


class AnalyzeClaimTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            tracking_id="T1", monthly_rent=Decimal("1450.00"), lease_start_date=date(2021, 12, 24),
            lease_end_date=date(2022, 12, 23), move_out_date=date(2022, 3, 1), termination_type="Early",
            claim_amount=Decimal("1775.00"), pm_explanation="Tenant left carpet damage.",
        )

    def make_client(self, scenario):
        no_wait = MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        return FakeGeminiClient(responses_dir=FIXTURES / scenario, limiter=no_wait, sleep=lambda s: None)

    def add(self, name, doc_type="other", mime="application/pdf", converted=""):
        return ClaimDocument.objects.create(
            claim=self.claim, file_path=f"claims/T1/{name}", file_name=name, file_hash=f"hash-{name}",
            mime_type=mime, doc_type=doc_type, converted_pdf_path=converted,
        )

    def lease(self, **terms):
        return LeaseExtraction.objects.create(
            claim=self.claim, is_lease=True, prompt_version="lease-v1", model_used="m", cache_key="lease-key",
            raw_json={"document_check": {"is_lease": True}, **terms},
        )

    def analyze(self, client, lease=None, **kwargs):
        return analyze_claim(self.claim, lease, client=client, template=TEMPLATE, schema=ClaimExtractionSchema, **kwargs)

    def test_placeholders_are_filled_from_claim(self):
        self.add("ledger.pdf", doc_type="ledger")
        self.add("Lease with charges.pdf", doc_type="lease_mixed")
        client = self.make_client("claim_valid")

        self.analyze(client, lease=self.lease(pet_deposit="refundable"))

        prompt = client.calls[0]["prompt"]
        self.assertNotIn("{{", prompt)
        self.assertIn("Claim T1", prompt)
        self.assertIn("Rent: 1450.00 | Lease: 2021-12-24 -> 2022-12-23 | Move-out: 2022-03-01", prompt)
        self.assertIn("Termination: Early | Claimed: 1775.00", prompt)
        self.assertIn("PM says: Tenant left carpet damage.", prompt)
        self.assertIn("- ledger.pdf (ledger)\n- Lease with charges.pdf (lease_mixed)", prompt)
        self.assertIn('"pet_deposit": "refundable"', prompt)
        self.assertNotIn("document_check", prompt)

    def test_no_lease(self):
        client = self.make_client("claim_valid")
        self.analyze(client)
        self.assertIn(f"Lease terms:\n{NO_LEASE}", client.calls[0]["prompt"])

    def test_missing_claim_values_are_not_provided(self):
        self.claim.monthly_rent = None
        self.claim.save()
        client = self.make_client("claim_valid")
        self.analyze(client)
        self.assertIn("Rent: Not provided |", client.calls[0]["prompt"])

    def test_no_claim_outcome_data_reaches_the_prompt(self):
        outcome = ClaimOutcome.objects.create(
            claim=self.claim, approved_benefit=Decimal("8765.43"), status="OUTCOME-STATUS-X",
            approval_date=date(2031, 7, 19), posted_date=date(2031, 8, 21),
            pending_docs_from_pm="OUTCOME-PENDING-X", hold_reason="OUTCOME-HOLD-X",
            tenant_contacted="OUTCOME-CONTACTED-X", tenant_collection_status="OUTCOME-TCS-X",
            collection_status="OUTCOME-CS-X", send_to_collections=True,
            agreed_tenant_settlement=Decimal("4321.09"), agreed_tenant_settlement_date=date(2031, 9, 3),
            collected_date=date(2031, 10, 5), collected_amount=Decimal("1234.56"),
            collection_processed_date=date(2031, 11, 7), audit_selection=True,
            raw_outcome={"Approved Benefit Amount": "$8,765.43", "Status": "OUTCOME-RAW-X"},
        )
        self.add("ledger.pdf", doc_type="ledger")
        client = self.make_client("claim_valid")

        self.analyze(client, lease=self.lease())

        prompt = client.calls[0]["prompt"]
        leaked = []
        for field in ClaimOutcome._meta.concrete_fields:
            value = getattr(outcome, field.attname)
            if field.name == "claim" or value is None or isinstance(value, bool):
                continue
            candidates = [str(value)] if not isinstance(value, dict) else [str(v) for v in value.values()]
            if hasattr(value, "isoformat"):
                candidates.append(value.isoformat())
            leaked += [f"{field.name}={c}" for c in candidates if c in prompt]
        self.assertEqual(leaked, [], f"Datos de ClaimOutcome en el prompt: {leaked}")
        self.assertNotIn("8,765.43", prompt)

    def test_documents_skip_lease_and_use_converted_pdf(self):
        self.add("Lease.pdf", doc_type="lease")
        self.add("Lease plus ledger.pdf", doc_type="lease_mixed")
        self.add("invoice.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 converted="_converted/T1/invoice.pdf")
        self.add("IMG_1.jpg", doc_type="photo", mime="image/jpeg")
        self.add("notes.msg", mime="application/vnd.ms-outlook")  # no se puede mandar a Gemini
        client = self.make_client("claim_valid")

        self.analyze(client)

        sent = [(f.path, f.mime_type) for f in client.calls[0]["files"]]
        self.assertEqual(sent, [
            ("/data/claims/T1/Lease plus ledger.pdf", "application/pdf"),
            ("/data/_converted/T1/invoice.pdf", "application/pdf"),
            ("/data/claims/T1/IMG_1.jpg", "image/jpeg"),
        ])

    def test_extraction_is_saved(self):
        lease = self.lease()
        client = self.make_client("claim_valid")

        extraction = self.analyze(client, lease=lease)

        self.assertEqual(extraction.raw_json["line_items"][0]["description"], "Carpet cleaning")
        self.assertEqual((extraction.confidence, extraction.prompt_version), ("high", "claim-v1"))
        self.assertEqual(extraction.lease_extraction, lease)
        self.assertEqual(extraction.model_used, "gemini-3.5-flash-lite")

    def test_second_call_uses_cache(self):
        self.add("ledger.pdf")
        client = self.make_client("claim_valid")
        first = self.analyze(client)

        self.assertEqual(self.analyze(client).pk, first.pk)
        self.assertEqual(len(client.calls), 1)

    def test_cache_invalidated_by_claim_data_lease_or_model(self):
        client = self.make_client("claim_valid")
        self.analyze(client)

        self.claim.pm_explanation = "Updated explanation"
        self.claim.save()
        self.analyze(client)
        self.analyze(client, lease=self.lease())
        self.analyze(client, model="gemini-3.5-flash")

        self.assertEqual(len(client.calls), 4)
        self.assertEqual(ClaimExtraction.objects.filter(claim=self.claim).count(), 4)
        self.assertEqual(client.calls[-1]["purpose"], "retry")

    def test_inverse_guard_reclassifies_lease_without_rerunning(self):
        doc = self.add("Bell Lease.pdf", doc_type="other")
        client = self.make_client("claim_lease_flag")

        self.analyze(client)

        doc.refresh_from_db()
        self.assertEqual((doc.doc_type, doc.classification_method), ("lease", "prompt_b_guard"))
        self.assertEqual(len(client.calls), 1)  # no se re-ejecuta nada

    def test_inverse_guard_ignored_when_claim_had_lease(self):
        doc = self.add("Bell Lease.pdf", doc_type="other")
        client = self.make_client("claim_lease_flag")

        self.analyze(client, lease=self.lease())

        doc.refresh_from_db()
        self.assertEqual(doc.doc_type, "other")

    def test_inverse_guard_identifies_document_from_findings(self):
        named = self.add("Bell Lease.pdf", doc_type="other")
        other = self.add("Addendum.pdf", doc_type="other")
        client = self.make_client("claim_lease_flag")  # finding: "Bell Lease.pdf is a residential lease..."

        self.analyze(client)

        named.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(named.doc_type, "lease")
        self.assertEqual(other.doc_type, "other")

    def test_inverse_guard_falls_back_to_lease_signals(self):
        scan = self.add("scan_0042.pdf", doc_type="other")
        ClaimDocument.objects.filter(pk=scan.pk).update(
            first_pages_text="RESIDENTIAL LEASE AGREEMENT between landlord and tenant. TERM OF LEASE: 12 months.")
        ledger = self.add("ledger.pdf", doc_type="ledger")
        client = self.make_client("claim_lease_flag_unnamed")  # flag sin findings

        self.analyze(client)

        scan.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual((scan.doc_type, scan.classification_method), ("lease", "prompt_b_guard"))
        self.assertEqual(ledger.doc_type, "ledger")

    def test_inverse_guard_does_nothing_when_ambiguous(self):
        a = self.add("a.pdf", doc_type="other")
        b = self.add("b.pdf", doc_type="other")
        client = self.make_client("claim_lease_flag_unnamed")

        with self.assertLogs("claims.pipeline.claim", level="WARNING"):
            self.analyze(client)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.doc_type, b.doc_type), ("other", "other"))


class RealPromptTests(TestCase):
    """claims/prompts/claim.md tal cual, sin plantilla de prueba."""

    def test_real_prompt_is_filled_without_outcome_data(self):
        claim = Claim.objects.create(tracking_id="1", monthly_rent=Decimal("1450.00"), claim_amount=Decimal("1.00"),
                                     move_out_date=date(2022, 3, 1))
        ClaimOutcome.objects.create(claim=claim, approved_benefit=Decimal("8765.43"), hold_reason="OUTCOME-HOLD-X")
        ClaimDocument.objects.create(claim=claim, file_path="claims/d/l.pdf", file_name="tenant ledger.pdf",
                                     file_hash="h", mime_type="application/pdf", doc_type="ledger")
        client = FakeGeminiClient(responses_dir=FIXTURES / "claim_valid",
                                  limiter=MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None))

        analyze_claim(claim, None, client=client)

        prompt = client.calls[0]["prompt"]
        self.assertTrue(prompt.startswith("You are an insurance claims analyst"))
        self.assertNotIn("{{", prompt)
        self.assertIn("- Tracking number: 1\n- Monthly rent: 1450.00\n- Lease start date: Not provided", prompt)
        self.assertIn("- Amount claimed by the property manager: 1.00", prompt)
        self.assertIn("# Documents received\n\n- tenant ledger.pdf (ledger)", prompt)
        self.assertIn(f"# Lease terms\n\n{NO_LEASE}", prompt)
        self.assertNotIn("8765.43", prompt)
        self.assertNotIn("OUTCOME-HOLD-X", prompt)
