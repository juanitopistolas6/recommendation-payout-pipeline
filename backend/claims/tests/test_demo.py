"""Paquete demo: export_demo (con pg_dump real contra la BD de pruebas).

    docker compose exec backend python manage.py test claims.tests.test_demo
"""

import json
import shutil
import subprocess
import tempfile
import zipfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from claims import demo
from claims.models import (
    Claim,
    ClaimDocument,
    ClaimExtraction,
    LeaseExtraction,
    LLMCall,
    Recommendation,
)


def processed_claim(tracking: str) -> Claim:
    """Un claim con todo lo que deja el pipeline: documento, extracciones, recomendación y una llamada al LLM."""
    claim = Claim.objects.create(tracking_id=tracking, processing_status=Claim.ProcessingStatus.DONE)
    doc = ClaimDocument.objects.create(claim=claim, file_path=f"claims/{tracking}/ledger.pdf", file_name="ledger.pdf",
                                       file_hash=f"h{tracking}", mime_type="application/pdf")
    lease = LeaseExtraction.objects.create(claim=claim, prompt_version="lease-v1", model_used="m", cache_key="k")
    lease.documents.add(doc)
    extraction = ClaimExtraction.objects.create(claim=claim, lease_extraction=lease, prompt_version="claim-v1",
                                                model_used="m", cache_key="k")
    Recommendation.objects.create(claim=claim, extraction=extraction, recommended_amount=Decimal("100.00"),
                                  eligible_owed=Decimal("100.00"), excluded_total=Decimal("0.00"),
                                  route="auto", rules_version="v1")
    LLMCall.objects.create(model="m", purpose="claim", claim=claim, status="ok")
    return claim


class ExportDemoTests(TransactionTestCase):
    """TransactionTestCase: pg_dump es otro proceso y solo ve datos confirmados; además export_demo abre su
    propia transacción REPEATABLE READ, que no puede ir dentro de la transacción de un TestCase."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(DATA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)

        processed_claim("2")
        processed_claim("10")
        Claim.objects.create(tracking_id="3")  # sin procesar
        converted = self.root / "_converted" / "2" / "Ledger.docx.pdf"
        converted.parent.mkdir(parents=True)
        converted.write_bytes(b"%PDF-1.4 demo")
        self.output = self.root / "demo" / "claims-demo.zip"

    def export(self, *args):
        out = StringIO()
        call_command("export_demo", "--output", str(self.output), *args, stdout=out)
        return out.getvalue()

    def test_zip_has_manifest_dump_and_converted_files(self):
        text = self.export()

        with zipfile.ZipFile(self.output) as zf:
            self.assertEqual(sorted(zf.namelist()), ["_converted/2/Ledger.docx.pdf", "db.dump", "manifest.json"])
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(zf.read("_converted/2/Ledger.docx.pdf"), b"%PDF-1.4 demo")
            dump = self.root / "db.dump"
            dump.write_bytes(zf.read("db.dump"))

        self.assertEqual(manifest["counts"], {"claims": 3, "claim_documents": 2, "lease_extractions": 2,
                                              "claim_extractions": 2, "recommendations": 2, "llm_calls": 2})
        self.assertEqual(manifest["counts"], demo.counts())
        self.assertEqual(manifest["recommended_tracking_numbers"], ["2", "10"])  # orden natural, no "10" < "2"
        self.assertEqual(manifest["rules_versions"], ["v1"])
        self.assertEqual(manifest["prompt_versions"], {"lease": ["lease-v1"], "claim": ["claim-v1"]})
        self.assertEqual(manifest["converted"], {"files": 1, "bytes": len(b"%PDF-1.4 demo")})
        self.assertEqual(manifest["postgres"]["major"], demo.server_version()["major"])
        self.assertIn("exported_at", manifest)

        toc = subprocess.run(["pg_restore", "--list", str(dump)], capture_output=True, text=True, check=True).stdout
        self.assertIn("TABLE DATA public recommendation", toc)
        self.assertNotIn("TABLE DATA public django_session", toc)  # la tabla sí, sus datos no
        self.assertIn("TABLE public django_session", toc)

        self.assertIn("claims-demo.zip", text)
        self.assertIn("Claims con recomendación: 2 (2, 10)", text)
        self.assertFalse(self.output.with_name("claims-demo.zip.partial").exists())

    def test_git_commit_from_environment(self):
        with mock.patch.dict("os.environ", {"GIT_COMMIT": "abc1234"}):
            self.export()
        with zipfile.ZipFile(self.output) as zf:
            self.assertEqual(json.loads(zf.read("manifest.json"))["git_commit"], "abc1234")

    def test_output_must_be_a_zip(self):
        with self.assertRaisesMessage(CommandError, "--output debe terminar en .zip"):
            call_command("export_demo", "--output", str(self.root / "demo.tar"), stdout=StringIO())

    def test_old_pg_dump_is_rejected_before_writing_anything(self):
        with mock.patch("claims.demo.tool_major", return_value=17), \
                mock.patch("claims.demo.server_version", return_value={"version": "18.6", "major": 18}), \
                self.assertRaisesMessage(CommandError, "pg_dump 17 es más viejo que el servidor Postgres 18"):
            self.export()
        self.assertFalse(self.output.exists())


class DemoHelperTests(SimpleTestCase):
    def test_tool_major(self):
        result = subprocess.CompletedProcess([], 0, stdout="pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg13+2)\n")
        with mock.patch("claims.demo.subprocess.run", return_value=result):
            self.assertEqual(demo.tool_major("pg_dump"), 18)

    def test_missing_tool(self):
        with mock.patch("claims.demo.subprocess.run", side_effect=FileNotFoundError), \
                self.assertRaisesMessage(demo.DemoError, "pg_restore no está instalado"):
            demo.tool_major("pg_restore")

    def test_human_size(self):
        self.assertEqual([demo.human_size(n) for n in (512, 2048, 37923816)], ["512 B", "2.0 KB", "36.2 MB"])


class ImportDemoTests(TransactionTestCase):
    """Ida y vuelta con pg_dump y pg_restore reales sobre la BD de pruebas."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(DATA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)

        processed_claim("2")
        processed_claim("10")
        self.converted = self.root / "_converted" / "2" / "Ledger.docx.pdf"
        self.converted.parent.mkdir(parents=True)
        self.converted.write_bytes(b"%PDF-1.4 demo")
        original = self.root / "claims" / "2" / "ledger.pdf"  # solo el original del claim 2 está en disco
        original.parent.mkdir(parents=True)
        original.write_bytes(b"%PDF-1.4")
        self.package = self.root / "demo" / "claims-demo.zip"
        call_command("export_demo", "--output", str(self.package), stdout=StringIO())

    def import_demo(self, *args, package=None):
        out = StringIO()
        call_command("import_demo", str(package or self.package), *args, stdout=out)
        return out.getvalue()

    def test_restores_database_and_converted_files(self):
        Recommendation.objects.all().delete()
        Claim.objects.create(tracking_id="99")  # no estaba en el paquete: desaparece
        self.converted.write_bytes(b"cambiado")

        text = self.import_demo("--force")

        self.assertEqual(demo.counts(), {"claims": 2, "claim_documents": 2, "lease_extractions": 2,
                                         "claim_extractions": 2, "recommendations": 2, "llm_calls": 2})
        self.assertFalse(Claim.objects.filter(pk="99").exists())
        self.assertEqual(self.converted.read_bytes(), b"%PDF-1.4 demo")  # sobrescrito con el del paquete
        self.assertIn("Claims con resultados: 2", text)
        self.assertIn("1 archivos extraídos", text)
        self.assertIn("Documentos originales en disco: 1 de 2", text)
        self.assertIn("Faltan 1 archivos originales; descarga la carpeta de documentos en data/claims", text)
        self.assertNotIn("el manifest dice", text)

    def test_existing_claims_without_force_change_nothing(self):
        Recommendation.objects.all().delete()  # sin resultados, pero con claims y documentos registrados
        self.converted.write_bytes(b"cambiado")

        with self.assertRaisesMessage(CommandError, "ya tiene 2 claims (0 con recomendaciones)"):
            self.import_demo()

        self.assertEqual((Claim.objects.count(), Recommendation.objects.count()), (2, 0))
        self.assertEqual(self.converted.read_bytes(), b"cambiado")

    def test_force_replaces_existing_results(self):
        Recommendation.objects.filter(claim_id="10").delete()

        text = self.import_demo("--force")

        self.assertEqual(Recommendation.objects.count(), 2)
        self.assertIn("Se reemplazó una base que tenía 2 claims (1 con recomendaciones)", text)

    def test_empty_database_needs_no_force(self):
        Claim.objects.all().delete()  # instalación nueva: migrada pero vacía

        text = self.import_demo()

        self.assertEqual(Claim.objects.count(), 2)
        self.assertNotIn("Se reemplazó", text)

    def test_older_server_is_rejected(self):
        with mock.patch("claims.demo.server_version", return_value={"version": "17.2", "major": 17}), \
                self.assertRaisesMessage(CommandError, "este servidor es 17.2"):
            self.import_demo("--force")
        self.assertEqual(Recommendation.objects.count(), 2)

    def broken_package(self, name, entries: dict):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as zf:
            for entry, content in entries.items():
                zf.writestr(entry, content)
        return path

    def test_invalid_packages(self):
        manifest = json.dumps({"format_version": 1, "postgres": {"major": 18}, "counts": {}, "exported_at": "x"})
        cases = [
            (self.root / "missing.zip", "No existe el archivo"),
            (self.broken_package("no-zip.zip", {}), "le falta manifest.json, db.dump"),
            (self.broken_package("no-dump.zip", {"manifest.json": manifest}), "le falta db.dump"),
            (self.broken_package("evil.zip", {"manifest.json": manifest, "db.dump": "x", "_converted/../../x": "x"}),
             "Rutas no permitidas"),
            (self.broken_package("v2.zip", {"manifest.json": '{"format_version": 2}', "db.dump": "x"}),
             "Formato de paquete 2 no soportado"),
        ]
        not_a_zip = self.root / "text.zip"
        not_a_zip.write_text("hola")
        cases.append((not_a_zip, "no es un ZIP válido"))
        for package, message in cases:
            with self.subTest(package=package.name), self.assertRaisesMessage(CommandError, message):
                self.import_demo("--force", package=package)
        self.assertEqual(Recommendation.objects.count(), 2)
