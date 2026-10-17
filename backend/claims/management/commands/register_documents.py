"""Registra los archivos de data/claims/<tracking_id>/ como ClaimDocument. No llama a ningún LLM.

Uso:
    docker compose exec backend python manage.py register_documents --ids 1,2,3
    docker compose exec backend python manage.py register_documents --all --workers 8

Es idempotente: correrlo dos veces no duplica documentos (se compara el hash SHA-256 por claim).
--workers N procesa N claims a la vez en procesos separados: extraer el texto de ~40,000 páginas
con pdfplumber en un solo proceso tarda más de una hora.
"""

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from claims import paths
from claims.documents.registry import RegistrationReport, register_by_tracking_id
from claims.models import Claim


class Command(BaseCommand):
    help = "Vincula cada archivo de data/claims/<tracking_id>/ con su claim y lo deja listo para Gemini."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--ids", help="Tracking IDs separados por coma, ej. 1,2,3")
        target.add_argument("--all", action="store_true", help="Todas las carpetas de data/claims/")
        parser.add_argument("--workers", type=int, default=1, help="Claims en paralelo (procesos). Default: 1")

    def handle(self, *args, **opts):
        if opts["workers"] < 1:
            raise CommandError("--workers debe ser 1 o más")
        if opts["ids"]:
            ids = list(dict.fromkeys(i.strip() for i in opts["ids"].split(",") if i.strip()))
            existing = set(Claim.objects.filter(pk__in=ids).values_list("pk", flat=True))
            missing = [i for i in ids if i not in existing]
            if missing:
                raise CommandError(f"No existen los claims: {', '.join(missing)}")
        else:
            root = paths.claims_dir()
            folders = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
            existing = set(Claim.objects.filter(pk__in=folders).values_list("pk", flat=True))
            orphans = sorted(folders - existing)
            if orphans:
                self.stdout.write(self.style.WARNING(f"Carpetas sin claim en la BD (se ignoran): {', '.join(orphans)}"))
            ids = sorted(existing, key=lambda t: (len(t), t))

        verbose = bool(opts["ids"])
        totals, unsupported = Counter(), Counter()
        no_folder, errors = [], []
        for done, report in enumerate(self._reports(ids, opts["workers"]), 1):
            if not report.folder_found:
                no_folder.append(report.tracking_id)
                continue
            totals.update(registered=report.registered, skipped=report.skipped, converted=report.converted)
            unsupported.update(Path(name).suffix.lower() or "(sin extensión)" for name in report.unsupported)
            errors += [f"{report.tracking_id}/{e}" for e in report.errors]
            if verbose or report.errors:
                self.stdout.write(f"  {report.tracking_id}: {report.registered} registrados, {report.skipped} saltados, "
                                  f"{report.converted} convertidos, {len(report.unsupported)} no soportados, "
                                  f"{len(report.errors)} errores")
            if not verbose and done % 50 == 0:
                self.stdout.write(f"  ... {done}/{len(ids)} claims")
                self.stdout.flush()

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(ids) - len(no_folder)} claims: {totals['registered']} archivos registrados, "
            f"{totals['skipped']} saltados (ya registrados o duplicados), {totals['converted']} convertidos, "
            f"{sum(unsupported.values())} no soportados, {len(errors)} errores"
        ))
        if unsupported:
            self.stdout.write("No soportados por tipo: " + ", ".join(f"{ext} {n}" for ext, n in unsupported.most_common()))
        if no_folder:
            self.stdout.write(self.style.WARNING(f"Sin carpeta en data/claims/: {', '.join(sorted(no_folder))}"))
        for error in errors:
            self.stdout.write(self.style.ERROR(f"  Error {error}"))

    def _reports(self, ids: list[str], workers: int):
        """Un RegistrationReport por claim; en paralelo si workers > 1 (en el orden en que terminan)."""
        if workers == 1:
            for tracking_id in ids:
                yield register_by_tracking_id(tracking_id)
            return

        connections.close_all()  # cada proceso hijo abre su propia conexión a Postgres
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("fork")) as pool:
            futures = {pool.submit(register_by_tracking_id, t): t for t in ids}
            for future in as_completed(futures):
                try:
                    yield future.result()
                except Exception as exc:  # noqa: BLE001 — un claim que revienta no detiene al resto
                    report = RegistrationReport(futures[future])
                    report.errors.append(f"(claim completo): {type(exc).__name__}: {exc}")
                    yield report
