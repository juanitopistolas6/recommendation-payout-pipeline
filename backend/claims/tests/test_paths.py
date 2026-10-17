"""Rutas portables: relativas a DATA_ROOT en la BD, migración de las absolutas y relocate_converted.

    docker compose exec backend python manage.py test claims.tests.test_paths
"""

import importlib
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from claims.models import Claim, ClaimDocument
from claims.paths import converted_relative_path, resolve_data_path, to_relative

migration = importlib.import_module("claims.migrations.0007_relative_document_paths")


@override_settings(DATA_ROOT="/data")
class PathTests(SimpleTestCase):
    def test_resolve(self):
        self.assertEqual(resolve_data_path("claims/1/Move Out/lease.pdf"), Path("/data/claims/1/Move Out/lease.pdf"))

    def test_resolve_rejects_absolute_windows_and_escaping_paths(self):
        for bad in ("", "/data/claims/1/a.pdf", "C:/data/a.pdf", "claims\\1\\a.pdf", "claims/../../etc/passwd"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_data_path(bad)

    def test_to_relative(self):
        self.assertEqual(to_relative("/data/claims/1/a b.pdf"), "claims/1/a b.pdf")
        with self.assertRaises(ValueError):
            to_relative("/tmp/claims/1/a.pdf")

    def test_converted_path_keeps_the_original_extension(self):
        self.assertEqual(converted_relative_path("claims/764/Ledger.docx", ".pdf"), "_converted/764/Ledger.docx.pdf")
        self.assertEqual(converted_relative_path("claims/764/Ledger.xlsx", ".pdf"), "_converted/764/Ledger.xlsx.pdf")
        self.assertEqual(converted_relative_path("claims/1/Fotos/IMG_1.HEIC", ".jpg"), "_converted/1/Fotos/IMG_1.HEIC.jpg")
        with self.assertRaises(ValueError):
            converted_relative_path("_converted/1/a.pdf", ".pdf")


@override_settings(DATA_ROOT="/data")
class MigrationTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(tracking_id="764")

    def doc(self, file_path, converted="", name=None):
        name = name or file_path.rsplit("/", 1)[-1]
        return ClaimDocument.objects.create(claim=self.claim, file_path=file_path, converted_pdf_path=converted,
                                            file_name=name, file_hash=name)

    def test_absolute_paths_become_relative(self):
        a = self.doc("/data/claims/764/Ledger.docx", "/data/_converted/764/a701a16c3db9cf2a/Ledger.pdf")
        b = self.doc("/data/claims/764/scan.pdf")
        c = self.doc("claims/764/already.pdf")  # ya relativa: no se toca

        migration.to_relative(apps, None)

        self.assertEqual(
            list(ClaimDocument.objects.filter(id__in=[a.id, b.id, c.id]).order_by("id")
                 .values_list("file_path", "converted_pdf_path")),
            [("claims/764/Ledger.docx", "_converted/764/a701a16c3db9cf2a/Ledger.pdf"),
             ("claims/764/scan.pdf", ""), ("claims/764/already.pdf", "")],
        )

    def test_paths_outside_data_root_stop_the_migration_and_change_nothing(self):
        ok = self.doc("/data/claims/764/ok.pdf")
        self.doc("/home/juan/claims/764/a.pdf")
        self.doc("C:\\Users\\juan\\b.pdf")

        with self.assertRaises(RuntimeError) as ctx:
            migration.to_relative(apps, None)

        self.assertIn("2 rutas no empiezan con DATA_ROOT", str(ctx.exception))
        self.assertIn("/home/juan/claims/764/a.pdf", str(ctx.exception))
        self.assertIn("C:\\Users\\juan\\b.pdf", str(ctx.exception))
        ok.refresh_from_db()
        self.assertEqual(ok.file_path, "/data/claims/764/ok.pdf")

    def test_reverse(self):
        d = self.doc("claims/764/Ledger.docx", "_converted/764/Ledger.docx.pdf")
        migration.to_absolute(apps, None)
        d.refresh_from_db()
        self.assertEqual((d.file_path, d.converted_pdf_path),
                         ("/data/claims/764/Ledger.docx", "/data/_converted/764/Ledger.docx.pdf"))


class RelocateConvertedTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(DATA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)
        self.claim = Claim.objects.create(tracking_id="764")

    def old_layout(self, name, content, hash_dir):
        old = self.root / "_converted" / "764" / hash_dir / f"{Path(name).stem}.pdf"
        old.parent.mkdir(parents=True)
        old.write_bytes(content)
        return ClaimDocument.objects.create(
            claim=self.claim, file_path=f"claims/764/{name}", file_name=name, file_hash=hash_dir,
            converted_pdf_path=old.relative_to(self.root).as_posix(),
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("relocate_converted", *args, stdout=out)
        return out.getvalue()

    def test_moves_files_updates_rows_and_removes_empty_folders(self):
        docx = self.old_layout("Ledger.docx", b"from docx", "aaaa")
        xlsx = self.old_layout("Ledger.xlsx", b"from xlsx", "bbbb")  # mismo nombre: antes no chocaban por el hash

        self.assertIn("Se moverían: 2", self.run_command("--dry-run"))
        self.assertTrue((self.root / "_converted/764/aaaa/Ledger.pdf").exists())  # el dry-run no toca nada

        text = self.run_command()

        self.assertIn("Movidos: 2. Ya en su lugar: 0. Carpetas vacías borradas: 2. Problemas: 0", text)
        docx.refresh_from_db()
        xlsx.refresh_from_db()
        self.assertEqual((docx.converted_pdf_path, xlsx.converted_pdf_path),
                         ("_converted/764/Ledger.docx.pdf", "_converted/764/Ledger.xlsx.pdf"))
        self.assertEqual(resolve_data_path(docx.converted_pdf_path).read_bytes(), b"from docx")
        self.assertEqual(resolve_data_path(xlsx.converted_pdf_path).read_bytes(), b"from xlsx")
        self.assertEqual(sorted(p.name for p in (self.root / "_converted/764").iterdir()),
                         ["Ledger.docx.pdf", "Ledger.xlsx.pdf"])

        self.assertIn("Movidos: 0. Ya en su lugar: 2", self.run_command())  # idempotente

    def test_missing_file_is_reported_and_row_is_kept(self):
        doc = self.old_layout("Lease.docx", b"x", "cccc")
        resolve_data_path(doc.converted_pdf_path).unlink()

        text = self.run_command()

        self.assertIn("Problemas: 1", text)
        self.assertIn("no existe _converted/764/cccc/Lease.pdf", text)
        doc.refresh_from_db()
        self.assertEqual(doc.converted_pdf_path, "_converted/764/cccc/Lease.pdf")
