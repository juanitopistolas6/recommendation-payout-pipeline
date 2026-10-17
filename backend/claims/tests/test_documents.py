"""Tests del registro de documentos (Sección 2). Usan LibreOffice real (está en la imagen del backend).

    docker compose exec backend python manage.py test claims.tests.test_documents
"""

import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import openpyxl
from django.core.management import call_command
from django.test import TestCase, override_settings
from docx import Document
from PIL import Image
from pypdf import PdfWriter

from claims.documents import registry
from claims.documents.registry import register_claim_documents
from claims.models import Claim, ClaimDocument

LEASE_PARAGRAPH = ("This Residential Lease Agreement is made between the Landlord and the Tenant for the premises "
                   "located at 305 Coosawatchie St. The monthly rent is $1,450.00 due on the first of the month. ")


class RegistryTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.claims_dir = self.root / "claims"
        self.converted_dir = self.root / "_converted"
        override = override_settings(DATA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)
        self.claim = Claim.objects.create(tracking_id="1")
        self.folder = self.claims_dir / "1"
        self.folder.mkdir(parents=True)

    # -- archivos de prueba ------------------------------------------------

    def scanned_pdf(self, name, pages=3):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=612, height=792)
        with (self.folder / name).open("wb") as fh:
            writer.write(fh)

    def docx(self, name, paragraphs=3):
        doc = Document()
        for _ in range(paragraphs):
            doc.add_paragraph(LEASE_PARAGRAPH * 3)
        doc.save(self.folder / name)

    def xlsx(self, name):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Description", "Charge", "Balance"])
        for i in range(1, 15):
            ws.append([f"03/{i:02d}/2022", f"Move out cleaning item {i}", 25.0 * i, 100.0 * i])
        wb.save(self.folder / name)

    def doc(self, name):
        return ClaimDocument.objects.get(claim=self.claim, file_name=name)

    # -- tests -------------------------------------------------------------

    def test_docx_is_converted_to_pdf_with_text(self):
        self.docx("Lease.docx")

        report = register_claim_documents(self.claim)

        self.assertEqual((report.registered, report.converted, report.errors), (1, 1, []))
        doc = self.doc("Lease.docx")
        self.assertEqual((doc.file_path, doc.converted_pdf_path), ("claims/1/Lease.docx", "_converted/1/Lease.docx.pdf"))
        converted = self.converted_dir / "1" / "Lease.docx.pdf"
        self.assertTrue(converted.exists())
        self.assertEqual(doc.mime_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn("Residential Lease Agreement", doc.first_pages_text)
        self.assertFalse(doc.has_scanned_pages)
        self.assertEqual(doc.llm_source, (str(converted), "application/pdf"))

    def test_scanned_pdf_is_flagged(self):
        self.scanned_pdf("Scan_0001.pdf", pages=3)

        register_claim_documents(self.claim)

        doc = self.doc("Scan_0001.pdf")
        self.assertEqual((doc.page_count, doc.has_scanned_pages, doc.converted_pdf_path), (3, True, ""))
        self.assertEqual(doc.mime_type, "application/pdf")

    def test_xlsx_doc_and_rtf_rmd_are_converted(self):
        self.xlsx("ledger.xlsx")
        (self.folder / "letter.rmd").write_text(r"{\rtf1\ansi No Refund Letter for 305 Coosawatchie St.\par}")

        report = register_claim_documents(self.claim)

        self.assertEqual((report.registered, report.converted, report.errors), (2, 2, []))
        ledger = self.doc("ledger.xlsx").full_text
        for i in range(1, 15):  # ninguna descripción recortada por el ancho de columna
            self.assertIn(f"Move out cleaning item {i}", ledger)
        rmd = self.doc("letter.rmd")
        self.assertIn("No Refund Letter", rmd.full_text)
        self.assertEqual(rmd.mime_type, "application/rtf")

    def test_rmd_that_is_not_rtf_is_unsupported(self):
        (self.folder / "analysis.rmd").write_text("---\ntitle: R Markdown\n---\n```{r}\n1 + 1\n```")
        report = register_claim_documents(self.claim)
        self.assertEqual((report.registered, report.unsupported), (0, ["analysis.rmd"]))

    def test_images(self):
        for name, color in (("IMG_1.JPG", "white"), ("invoice.jfif", "blue"), ("wall.png", "red")):  # distinto hash
            Image.new("RGB", (40, 30), color).save(self.folder / name, "JPEG" if name[-3:].upper() != "PNG" else "PNG")

        register_claim_documents(self.claim)

        self.assertEqual(
            sorted(ClaimDocument.objects.values_list("file_name", "mime_type", "page_count", "full_text")),
            [("IMG_1.JPG", "image/jpeg", 1, ""), ("invoice.jfif", "image/jpeg", 1, ""), ("wall.png", "image/png", 1, "")],
        )

    def test_heic_is_converted_to_jpg(self):
        try:
            from pillow_heif import from_pillow
            heif = from_pillow(Image.new("RGB", (64, 48), "red"))
            heif.save(self.folder / "photo.heic", quality=50)
        except Exception as exc:  # noqa: BLE001 — si la librería no trae codificador HEIC
            self.skipTest(f"No se pudo crear un HEIC de prueba: {exc}")

        report = register_claim_documents(self.claim)

        doc = self.doc("photo.heic")
        self.assertEqual((report.converted, doc.mime_type, doc.page_count), (1, "image/heic", 1))
        self.assertEqual(doc.converted_pdf_path, "_converted/1/photo.heic.jpg")
        self.assertEqual(doc.llm_source, (str(self.converted_dir / "1" / "photo.heic.jpg"), "image/jpeg"))
        with Image.open(self.converted_dir / "1" / "photo.heic.jpg") as img:
            self.assertEqual(img.size, (64, 48))

    def test_running_twice_does_not_duplicate_or_reconvert(self):
        self.docx("Lease.docx")
        self.scanned_pdf("scan.pdf")
        register_claim_documents(self.claim)

        with mock.patch("claims.documents.registry.subprocess.run") as soffice:
            report = register_claim_documents(self.claim)

        self.assertEqual((report.registered, report.skipped), (0, 2))
        self.assertEqual(ClaimDocument.objects.count(), 2)
        soffice.assert_not_called()

    def test_conversion_is_reused_if_document_was_deleted(self):
        self.docx("Lease.docx")
        register_claim_documents(self.claim)
        ClaimDocument.objects.all().delete()

        with mock.patch("claims.documents.registry.subprocess.run") as soffice:
            report = register_claim_documents(self.claim)

        self.assertEqual(report.registered, 1)
        soffice.assert_not_called()  # el PDF ya estaba en _converted/

    def test_same_content_with_two_names_is_registered_once(self):
        self.scanned_pdf("ledger.pdf")
        shutil.copyfile(self.folder / "ledger.pdf", self.folder / "ledger (1).pdf")

        report = register_claim_documents(self.claim)

        self.assertEqual((report.registered, report.skipped), (1, 1))

    def test_subfolders_are_included_with_relative_name(self):
        (self.folder / "Move Out Invoices").mkdir()
        self.scanned_pdf("Move Out Invoices/invoice.pdf")

        register_claim_documents(self.claim)

        doc = ClaimDocument.objects.get()
        self.assertEqual(doc.file_name, "Move Out Invoices/invoice.pdf")
        self.assertEqual(doc.file_path, "claims/1/Move Out Invoices/invoice.pdf")

    def test_corrupt_files_are_reported_and_do_not_stop_the_rest(self):
        (self.folder / "broken.pdf").write_bytes(b"%PDF-1.4 esto no es un pdf")
        (self.folder / "broken.docx").write_bytes(b"tampoco es un docx")
        self.scanned_pdf("ok.pdf")

        with self.assertLogs("claims.documents.registry", level="WARNING"):
            report = register_claim_documents(self.claim)

        self.assertEqual(report.registered, 1)
        self.assertEqual(sorted(e.split(":")[0] for e in report.errors), ["broken.docx", "broken.pdf"])
        self.assertEqual(list(ClaimDocument.objects.values_list("file_name", flat=True)), ["ok.pdf"])

    def test_unsupported_and_system_files(self):
        for name in ("docs.zip", "notes.txt", "mail.msg", "Thumbs.db", "~$Lease.docx"):
            (self.folder / name).write_bytes(b"x")

        report = register_claim_documents(self.claim)

        self.assertEqual(sorted(report.unsupported), ["docs.zip", "mail.msg", "notes.txt"])
        self.assertEqual(report.registered, 0)

    def test_claim_without_folder(self):
        claim = Claim.objects.create(tracking_id="2")
        self.assertFalse(register_claim_documents(claim).folder_found)

    def test_nul_characters_are_removed(self):
        self.assertEqual(registry._clean("a\x00b"), "ab")

    def test_command(self):
        self.scanned_pdf("scan.pdf")
        (self.folder / "x.zip").write_bytes(b"x")
        Claim.objects.create(tracking_id="2")
        out = StringIO()

        call_command("register_documents", "--ids", "1,2", stdout=out)
        call_command("register_documents", "--all", stdout=out)

        text = out.getvalue()
        self.assertIn("1: 1 registrados, 0 saltados, 0 convertidos, 1 no soportados, 0 errores", text)
        self.assertIn("Sin carpeta en data/claims/: 2", text)
        self.assertIn("0 archivos registrados, 1 saltados", text)  # segunda corrida
        self.assertIn("No soportados por tipo: .zip 1", text)
        self.assertEqual(ClaimDocument.objects.count(), 1)

    def test_command_with_workers(self):
        for tracking in ("2", "3"):
            Claim.objects.create(tracking_id=tracking)
            (self.claims_dir / tracking).mkdir()
        self.scanned_pdf("scan.pdf")
        out = StringIO()

        # En los tests todo corre en una transacción que otros procesos no ven: se simula el pool
        # Ni el pool ni el cierre de conexiones: la transacción del test tiene que seguir abierta
        with mock.patch("claims.management.commands.register_documents.ProcessPoolExecutor") as pool_cls, \
                mock.patch("claims.management.commands.register_documents.connections") as conns:
            from concurrent.futures import Future

            def submit(fn, *args):
                future = Future()
                future.set_result(fn(*args))
                return future

            pool_cls.return_value.__enter__.return_value.submit.side_effect = submit
            call_command("register_documents", "--all", "--workers", "4", stdout=out)

        pool_cls.assert_called_once()
        conns.close_all.assert_called_once()
        self.assertEqual(pool_cls.call_args.kwargs["max_workers"], 4)
        self.assertIn("3 claims: 1 archivos registrados", out.getvalue())
