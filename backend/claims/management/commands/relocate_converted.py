"""Mueve las conversiones al esquema derivado de la ruta del original y actualiza la BD:

    _converted/764/a701a16c3db9cf2a/Ledger.pdf  ->  _converted/764/Ledger.docx.pdf

Uso:
    docker compose exec backend python manage.py relocate_converted --dry-run
    docker compose exec backend python manage.py relocate_converted

Es idempotente: las que ya están en su lugar se saltan. Cada archivo se mueve y su fila se actualiza juntos;
si la fila no se puede guardar, el archivo vuelve a su lugar. Al final borra las carpetas que quedaron vacías.
"""

import os
from pathlib import PurePosixPath

from django.core.management.base import BaseCommand
from django.db import transaction

from claims import paths
from claims.models import ClaimDocument


class Command(BaseCommand):
    help = "Mueve data/_converted/ al esquema _converted/<ruta del original>.<ext> y actualiza converted_pdf_path."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo muestra lo que haría")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        moved = in_place = 0
        problems = []
        for doc in ClaimDocument.objects.exclude(converted_pdf_path="").order_by("id"):
            suffix = PurePosixPath(doc.converted_pdf_path).suffix
            expected = paths.converted_relative_path(doc.file_path, suffix)
            if doc.converted_pdf_path == expected:
                in_place += 1
                continue

            source, target = paths.resolve_data_path(doc.converted_pdf_path), paths.resolve_data_path(expected)
            if not source.exists():
                problems.append(f"{doc.id}: no existe {doc.converted_pdf_path}")
                continue
            if target.exists():
                problems.append(f"{doc.id}: ya existe {expected} (no se sobrescribe)")
                continue
            if dry_run:
                self.stdout.write(f"  {doc.converted_pdf_path} -> {expected}")
                moved += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            try:
                with transaction.atomic():
                    ClaimDocument.objects.filter(id=doc.id).update(converted_pdf_path=expected)
            except Exception:
                os.replace(target, source)
                raise
            moved += 1

        removed = 0 if dry_run else self._remove_empty_dirs()
        verb = "Se moverían" if dry_run else "Movidos"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {moved}. Ya en su lugar: {in_place}. Carpetas vacías borradas: {removed}. Problemas: {len(problems)}"
        ))
        for problem in problems:
            self.stdout.write(self.style.ERROR(f"  {problem}"))

    def _remove_empty_dirs(self) -> int:
        removed = 0
        root = paths.converted_dir()
        for folder, _, _ in sorted(os.walk(root), key=lambda w: -len(w[0])):  # de las más profundas hacia arriba
            if folder != str(root) and not os.listdir(folder):
                os.rmdir(folder)
                removed += 1
        return removed
