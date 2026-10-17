"""Tests de la clasificación de documentos (sin LLM).

    docker compose exec backend python manage.py test claims.tests.test_classify
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image
from pypdf import PdfReader, PdfWriter

from claims.models import Claim, ClaimDocument
from claims.pipeline.classify import (
    LLMDocResult,
    apply_llm_results,
    classify_by_extension,
    classify_by_rules,
    classify_claim_documents,
    decide_from_llm,
    documents_for_prompt_a,
    documents_for_prompt_b,
    first_page_file,
    prompts_for,
)

LEASE_TEXT = """
RESIDENTIAL LEASE AGREEMENT
This Residential Lease Agreement is entered into between Pine Island Properties LLC
("Landlord") and Liliana Bell ("Tenant"). The relationship of landlord and tenant is created
for the premises located at 153 Pine Island Rd. TERM OF LEASE: twelve (12) months beginning
January 1, 2022. Monthly rent is $1,450.00 due on the first day of each month.
"""

LEDGER_TEXT = """
TENANT LEDGER - Statement of Account
Resident: Paul Strickland   Property: 10205 Maydelle Street
Date        Description                 Charges    Payments   Balance
03/01/2022  Rent                        1,450.00              1,450.00
03/15/2022  Move out cleaning invoice     325.00              1,775.00
Total Due: $1,775.00        Balance Due: $1,775.00
"""

SCAN_TEXT = "  \n Page 1 \n  "  # PDF escaneado: pdfplumber casi no extrae texto


def doc(file_name="doc.pdf", text="", pages=1, full_text=None):
    return ClaimDocument(file_name=file_name, first_pages_text=text, page_count=pages,
                         full_text=text if full_text is None else full_text)


class ClassifyByRulesTests(SimpleTestCase):
    def test_lease(self):
        self.assertEqual(classify_by_rules(doc("Bell Lease.pdf", LEASE_TEXT, pages=12)), ("lease", "rules"))

    def test_lease_signals_in_text_without_lease_in_name(self):
        self.assertEqual(classify_by_rules(doc("contract.pdf", LEASE_TEXT, pages=12)), ("lease", "rules"))

    def test_lease_with_charges_at_the_end_is_mixed(self):
        text = LEASE_TEXT + LEDGER_TEXT
        full = LEASE_TEXT + ("clause text " * 500) + LEDGER_TEXT
        self.assertEqual(classify_by_rules(doc("Lease.pdf", text, pages=14, full_text=full)), ("lease_mixed", "rules"))

    def test_short_lease_is_ambiguous(self):
        # Un addendum de 2 páginas con señales de lease no alcanza el mínimo de 5 páginas
        self.assertEqual(classify_by_rules(doc("Lease Addendum.pdf", LEASE_TEXT, pages=2)), ("unknown", "ambiguous"))

    def test_ledger_is_charges(self):
        self.assertEqual(classify_by_rules(doc("tenant ledger.pdf", LEDGER_TEXT, pages=1)), ("charges", "rules"))

    def test_charges_mentioning_lease_in_name_are_ambiguous(self):
        self.assertEqual(classify_by_rules(doc("lease ledger.pdf", LEDGER_TEXT)), ("unknown", "ambiguous"))

    def test_scanned_pdf_has_no_text(self):
        self.assertEqual(classify_by_rules(doc("Scan_0001.pdf", SCAN_TEXT, pages=3)), ("unknown", "no_text"))


class ClassifyByExtensionTests(SimpleTestCase):
    def test_images_are_photos(self):
        for name in ["IMG_2040.jpg", "kitchen.JPEG", "wall.PNG", "door.heic"]:
            self.assertEqual(classify_by_extension(doc(name)), "photo", name)

    def test_other_files_are_not(self):
        for name in ["lease.pdf", "Demand Letter.GIF", "invoice.jfif", "notes.docx"]:
            self.assertIsNone(classify_by_extension(doc(name)), name)


class DecideFromLLMTests(SimpleTestCase):
    def r(self, doc_type, confidence, mixed=False):
        return decide_from_llm(LLMDocResult("1", doc_type, confidence, mixed))

    def test_lease_needs_high_or_medium(self):
        self.assertEqual(self.r("lease", "high"), "lease")
        self.assertEqual(self.r("lease", "medium"), "lease")
        self.assertEqual(self.r("lease", "low"), "other")

    def test_mixed_lease(self):
        self.assertEqual(self.r("lease", "high", mixed=True), "lease_mixed")

    def test_low_confidence_is_charges(self):
        self.assertEqual(self.r("invoice", "low"), "other")

    def test_known_types_are_kept(self):
        self.assertEqual(self.r("ledger", "high"), "ledger")
        self.assertEqual(self.r("inspection_report", "medium"), "inspection_report")

    def test_unknown_or_invented_types_become_other(self):
        self.assertEqual(self.r("unknown", "high"), "other")
        self.assertEqual(self.r("receipt", "high"), "other")


class ClaimDocumentsTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(tracking_id="T1")

    def add(self, name, text="", pages=1, doc_type="pending"):
        return ClaimDocument.objects.create(
            claim=self.claim, file_path=f"claims/T1/{name}", file_name=name, file_hash=name,
            first_pages_text=text, full_text=text, page_count=pages, doc_type=doc_type,
        )

    def test_extension_and_rules_without_llm(self):
        photo = self.add("IMG_1.jpg")
        lease = self.add("Lease.pdf", LEASE_TEXT, pages=10)
        ledger = self.add("ledger.pdf", LEDGER_TEXT)
        scan = self.add("Scan.pdf", SCAN_TEXT)
        done = self.add("old.pdf", doc_type="invoice")  # ya clasificado: no se toca

        classify_claim_documents(self.claim, use_llm=False)

        for d in (photo, lease, ledger, scan, done):
            d.refresh_from_db()
        self.assertEqual((photo.doc_type, photo.classification_method), ("photo", "extension"))
        self.assertEqual((lease.doc_type, lease.classification_method), ("lease", "rules"))
        self.assertEqual((ledger.doc_type, ledger.classification_method), ("other", "rules"))
        self.assertEqual((scan.doc_type, scan.classification_method), ("unknown", ""))
        self.assertEqual((done.doc_type, done.classification_method), ("invoice", ""))

    def test_llm_results_missing_ids_stay_unknown(self):
        a = self.add("a.pdf", doc_type="unknown")
        b = self.add("b.pdf", doc_type="unknown")

        missing = apply_llm_results([a, b], [LLMDocResult(str(a.id), "lease", "high", mixed=True)])

        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.doc_type, a.classification_method, a.classification_confidence), ("lease_mixed", "llm", "high"))
        self.assertEqual(b.doc_type, "unknown")
        self.assertEqual(missing, [b])

    def test_routing(self):
        lease = self.add("l.pdf", doc_type="lease")
        mixed = self.add("m.pdf", doc_type="lease_mixed")
        other = self.add("o.pdf", doc_type="other")
        unknown = self.add("u.pdf", doc_type="unknown")

        self.assertEqual(set(documents_for_prompt_a(self.claim)), {lease, mixed})
        self.assertEqual(set(documents_for_prompt_b(self.claim)), {mixed, other, unknown})
        self.assertEqual(prompts_for("lease"), {"A"})
        self.assertEqual(prompts_for("lease_mixed"), {"A", "B"})
        self.assertEqual(prompts_for("photo"), {"B"})


class FirstPageTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        override = override_settings(DATA_ROOT=str(self.tmp))  # las rutas de la BD son relativas a DATA_ROOT
        override.enable()
        self.addCleanup(override.disable)

    def test_pdf_first_page_only(self):
        source = self.tmp / "three.pdf"
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=612, height=792)
        with source.open("wb") as fh:
            writer.write(fh)
        d = ClaimDocument(id=7, file_name="three.pdf", file_path="three.pdf", mime_type="application/pdf")

        out = first_page_file(d, self.tmp)

        self.assertEqual(len(PdfReader(out.path).pages), 1)
        self.assertEqual(out.label, "Document ID: 7")

    def test_image_is_resized_to_1600(self):
        source = self.tmp / "big.png"
        Image.new("RGB", (4000, 3000), "white").save(source)
        d = ClaimDocument(id=8, file_name="big.png", file_path="big.png", mime_type="image/png")

        out = first_page_file(d, self.tmp)

        with Image.open(out.path) as img:
            self.assertEqual(img.size, (1600, 1200))
        self.assertEqual(out.mime_type, "image/jpeg")

    def test_corrupt_file_returns_none(self):
        source = self.tmp / "broken.pdf"
        source.write_bytes(b"not a pdf")
        d = ClaimDocument(id=9, file_name="broken.pdf", file_path="broken.pdf")

        self.assertIsNone(first_page_file(d, self.tmp))


class LLMStageTests(TestCase):
    """Etapa LLM con el Prompt C y ClassificationBatch reales, usando FakeGeminiClient."""

    def setUp(self):
        from claims.llm.fake import FakeGeminiClient
        from claims.llm.rate_limit import MinuteRateLimiter

        self.tmp = Path(tempfile.mkdtemp())
        override = override_settings(DATA_ROOT=str(self.tmp))
        override.enable()
        self.addCleanup(override.disable)
        self.claim = Claim.objects.create(tracking_id="C1")
        self.docs = [self.scanned_pdf(f"scan{i}.pdf") for i in range(3)]
        self.responses = self.tmp / "responses"
        self.responses.mkdir()
        self.client = FakeGeminiClient(responses_dir=self.responses,
                                       limiter=MinuteRateLimiter(clock=lambda: 0.0, sleep=lambda s: None))

    def scanned_pdf(self, name):
        path = self.tmp / name
        writer = PdfWriter()
        for _ in range(4):
            writer.add_blank_page(width=612, height=792)
        with path.open("wb") as fh:
            writer.write(fh)
        return ClaimDocument.objects.create(claim=self.claim, file_path=name, file_name=name, file_hash=name,
                                            mime_type="application/pdf", page_count=4, has_scanned_pages=True)

    def respond(self, documents):
        import json

        from claims.llm.schemas import ClassificationBatch

        body = {"documents": documents}
        ClassificationBatch.model_validate(body)
        (self.responses / "classify.json").write_text(json.dumps(body))

    def test_scanned_documents_go_to_llm_with_their_first_page(self):
        a, b, c = self.docs
        self.respond([
            {"id": str(a.id), "type": "lease", "mixed": True, "confidence": "high", "reason": "lease + ledger"},
            {"id": str(b.id), "type": "invoice", "mixed": False, "confidence": "low", "reason": "blurry"},
            # c no vuelve
        ])

        classify_claim_documents(self.claim, client=self.client)

        call = self.client.calls[0]
        self.assertTrue(call["prompt"].startswith("You are a document classifier"))
        self.assertEqual(call["model"], "gemini-3.1-flash-lite")
        self.assertEqual([f.label for f in call["files"]], [f"Document ID: {d.id}" for d in self.docs])
        for f in call["files"]:
            self.assertEqual(f.mime_type, "application/pdf")
        for d in self.docs:
            d.refresh_from_db()
        self.assertEqual((a.doc_type, a.classification_method, a.classification_confidence), ("lease_mixed", "llm", "high"))
        self.assertEqual((b.doc_type, b.classification_confidence), ("other", "low"))  # low -> cargos
        self.assertEqual((c.doc_type, c.classification_method), ("unknown", ""))  # faltó en la respuesta

    def test_batches_of_20(self):
        from unittest import mock

        self.respond([])
        with mock.patch("claims.pipeline.classify.LLM_BATCH_SIZE", 2):
            classify_claim_documents(self.claim, client=self.client)
        self.assertEqual([len(c["files"]) for c in self.client.calls], [2, 1])
