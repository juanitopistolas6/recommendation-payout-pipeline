"""Importa el Excel de claims a PostgreSQL.

Uso:
    docker compose exec backend python manage.py import_claims "Security Deposit Claims.xlsx"
    docker compose exec backend python manage.py import_claims "Security Deposit Claims.xlsx" --dry-run
    docker compose exec backend python manage.py import_claims "Security Deposit Claims.xlsx" --sheet "Claims"
    docker compose exec backend python manage.py import_claims "Security Deposit Claims.xlsx" --test-size 40 --seed 42

Es idempotente: correrlo dos veces actualiza los claims existentes (por Tracking Number)
en lugar de duplicarlos. El test set se asigna una sola vez; para rehacerlo usa --reset-test-set.
"""

import random
import re
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from claims.excel.clean_data import check_columns, parse_row, read_claims_file
from claims.models import Claim, ClaimOutcome


class DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = "Importa el Excel de claims (Claim + ClaimOutcome) y asigna el test set."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Ruta al .xlsx o .csv (dentro del contenedor, ej. /data/claims.xlsx)")
        parser.add_argument("--sheet", default=0, help="Nombre o índice de la hoja (default: la primera)")
        parser.add_argument("--dry-run", action="store_true", help="Parsea y reporta sin guardar nada")
        parser.add_argument("--test-size", type=int, default=40, help="Claims en el test set (default: 40)")
        parser.add_argument("--seed", type=int, default=42, help="Semilla del muestreo (default: 42)")
        parser.add_argument("--reset-test-set", action="store_true", help="Vuelve a muestrear el test set")
        parser.add_argument("--show-warnings", type=int, default=10, help="Cuántas advertencias de ejemplo mostrar")

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.exists():
            raise CommandError(f"No existe el archivo: {path}")

        sheet = int(opts["sheet"]) if str(opts["sheet"]).isdigit() else opts["sheet"]
        df = read_claims_file(path, sheet=sheet)
        self.stdout.write(f"Leídas {len(df)} filas y {len(df.columns)} columnas de {path.name}")

        missing, unknown = check_columns(list(df.columns))
        if missing:
            raise CommandError(f"Faltan columnas requeridas: {', '.join(missing)}")
        if unknown:
            self.stdout.write(self.style.WARNING(
                f"Columnas no mapeadas (se guardan solo en raw_row): {', '.join(unknown)}"
            ))

        stats = Counter()
        warnings_by_column = Counter()
        warning_examples = []
        seen = set()
        with_ground_truth = []  # claims con monto aprobado en el Excel (candidatos al test set)

        try:
            with transaction.atomic():
                for position, (_, row) in enumerate(df.iterrows()):
                    excel_row = position + 2  # +1 por el encabezado, +1 porque Excel cuenta desde 1
                    parsed = parse_row(row)
                    tracking = parsed.claim.get("tracking_id")

                    if not tracking:
                        stats["skipped_no_id"] += 1
                        continue
                    if tracking in seen:
                        stats["skipped_duplicate"] += 1
                        warning_examples.append(f"Fila {excel_row}: Tracking Number {tracking} repetido, se omite")
                        continue
                    seen.add(tracking)

                    for w in parsed.warnings:
                        warnings_by_column[w.split(":")[0]] += 1
                        if len(warning_examples) < opts["show_warnings"]:
                            warning_examples.append(f"Fila {excel_row} (#{tracking}) {w}")

                    claim_fields = {k: v for k, v in parsed.claim.items() if k != "tracking_id"}
                    claim, created = Claim.objects.update_or_create(
                        tracking_id=tracking,
                        defaults={
                            **claim_fields,
                            "raw_row": parsed.raw_row,
                            "source_file": path.name,
                            "source_row": excel_row,
                        },
                    )
                    # La decisión humana va aparte: el pipeline lee Claim y nunca ClaimOutcome
                    ClaimOutcome.objects.update_or_create(
                        claim=claim,
                        defaults={**parsed.outcome, "raw_outcome": parsed.raw_outcome},
                    )
                    stats["outcomes"] += 1
                    if parsed.outcome.get("approved_benefit") is not None:
                        with_ground_truth.append(tracking)
                    stats["created" if created else "updated"] += 1

                test_count = self._assign_test_set(
                    with_ground_truth, opts["test_size"], opts["seed"], opts["reset_test_set"], stats
                )

                if opts["dry_run"]:
                    raise DryRunRollback()
        except DryRunRollback:
            self.stdout.write(self.style.WARNING("--dry-run: no se guardó nada"))

        self._report(stats, warnings_by_column, warning_examples, test_count)

    # ------------------------------------------------------------------

    def _assign_test_set(self, candidates, size, seed, reset, stats):
        if reset:
            Claim.objects.update(is_test_set=False)
        elif Claim.objects.filter(is_test_set=True).exists():
            stats["test_set_kept"] = 1
            return Claim.objects.filter(is_test_set=True).count()

        # Solo claims con decisión humana: sin ground truth no se puede medir el MAE
        # Candidatos tomados del Excel en esta misma corrida (no depende de lo que ya haya en la BD)
        candidates = sorted(candidates, key=_natural_key)  # orden estable, independiente del orden de inserción
        chosen = random.Random(seed).sample(candidates, min(size, len(candidates)))
        Claim.objects.filter(tracking_id__in=chosen).update(is_test_set=True)
        return len(chosen)

    def _report(self, stats, warnings_by_column, warning_examples, test_count):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Creados: {stats['created']}  Actualizados: {stats['updated']}  "
            f"Sin Tracking Number: {stats['skipped_no_id']}  Duplicados: {stats['skipped_duplicate']}  "
            f"Resultados (ClaimOutcome): {stats['outcomes']}"
        ))
        kept = " (se conservó el existente; usa --reset-test-set para rehacerlo)" if stats["test_set_kept"] else ""
        self.stdout.write(f"Test set: {test_count} claims{kept}")

        if warnings_by_column:
            self.stdout.write(self.style.WARNING("\nValores que no se pudieron parsear (quedaron vacíos; el original está en raw_row):"))
            for column, count in warnings_by_column.most_common():
                self.stdout.write(f"  {column}: {count}")
        if warning_examples:
            self.stdout.write("\nEjemplos:")
            for w in warning_examples:
                self.stdout.write(f"  {w}")


def _natural_key(value: str):
    """'2' < '10' en lugar del orden de texto ('10' < '2')."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", value)]