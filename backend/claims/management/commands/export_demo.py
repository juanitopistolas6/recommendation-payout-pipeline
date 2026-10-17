"""Genera el paquete demo: la BD procesada y data/_converted/ en un ZIP (ver claims/demo.py).

Uso:
    docker compose exec backend python manage.py export_demo
    docker compose exec backend python manage.py export_demo --output /data/demo/claims-demo-2026-10-17.zip

Los conteos del manifest y el dump salen de la misma foto de la BD: se exporta un snapshot de Postgres
(REPEATABLE READ + pg_export_snapshot) y pg_dump lo usa con --snapshot. Si el worker escribe mientras tanto,
el manifest sigue coincidiendo con el dump.

El ZIP contiene datos personales de inquilinos: se comparte por un link privado, nunca se sube al repo.
"""

import json
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from claims import demo, paths

DEFAULT_OUTPUT = "/data/demo/claims-demo.zip"


class Command(BaseCommand):
    help = "Genera el paquete demo (manifest.json + db.dump + _converted/) para cargarlo con import_demo."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Ruta del ZIP. Default: {DEFAULT_OUTPUT}")

    def handle(self, *args, **opts):
        output = Path(opts["output"])
        if output.suffix.lower() != ".zip":
            raise CommandError("--output debe terminar en .zip")
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CommandError(f"No se puede escribir en {output.parent}: {exc}") from None

        try:
            server = demo.server_version()
            demo.check_tool("pg_dump", server["major"])
            with tempfile.TemporaryDirectory() as tmp:
                dump = Path(tmp) / demo.DUMP
                manifest = self._dump_with_manifest(dump, server)
                converted = demo.converted_files()
                manifest["converted"] = {"files": len(converted), "bytes": sum(p.stat().st_size for p in converted)}
                partial = output.with_name(output.name + ".partial")
                self._write_zip(partial, manifest, dump, converted)
                os.replace(partial, output)  # nunca queda un ZIP a medias con el nombre final
        except demo.DemoError as exc:
            raise CommandError(str(exc)) from None

        self._summary(output, manifest)

    def _dump_with_manifest(self, dump: Path, server: dict) -> dict:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SELECT pg_export_snapshot()")
                snapshot = cursor.fetchone()[0]
            manifest = {
                "format_version": demo.FORMAT_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "git_commit": git_commit(),
                "postgres": {**server, "pg_dump": demo.tool_major("pg_dump")},
                "counts": demo.counts(),
                "recommended_tracking_numbers": demo.recommended_tracking_numbers(),
                **demo.versions(),
            }
            self.stdout.write("Volcando la base de datos (pg_dump)...")
            self.stdout.flush()
            # La transacción sigue abierta mientras pg_dump usa su snapshot
            demo.run_pg(["pg_dump", "-Fc", "--no-owner", "--no-privileges",
                         "--exclude-table-data=django_session", f"--snapshot={snapshot}", "-f", str(dump)])
        return manifest

    def _write_zip(self, target: Path, manifest: dict, dump: Path, converted: list[Path]) -> None:
        self.stdout.write(f"Empaquetando {len(converted)} archivos de _converted/...")
        self.stdout.flush()
        root = paths.data_root()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(demo.MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            zf.write(dump, demo.DUMP, compress_type=zipfile.ZIP_STORED)  # -Fc ya viene comprimido
            for path in converted:
                zf.write(path, path.relative_to(root).as_posix())

    def _summary(self, output: Path, manifest: dict) -> None:
        counts = manifest["counts"]
        width = max(map(len, counts))
        self.stdout.write(self.style.SUCCESS(f"\nPaquete demo: {output} ({demo.human_size(output.stat().st_size)})"))
        for name, value in counts.items():
            self.stdout.write(f"  {name:<{width}}  {value:>6}")
        tracking = manifest["recommended_tracking_numbers"]
        self.stdout.write(f"Claims con recomendación: {len(tracking)}"
                          + (f" ({', '.join(tracking[:20])}{', ...' if len(tracking) > 20 else ''})" if tracking else ""))
        self.stdout.write(f"rules_version: {', '.join(manifest['rules_versions']) or '-'} · "
                          f"prompts: lease {', '.join(manifest['prompt_versions']['lease']) or '-'}, "
                          f"claim {', '.join(manifest['prompt_versions']['claim']) or '-'}")
        converted = manifest["converted"]
        self.stdout.write(f"_converted/: {converted['files']} archivos, {demo.human_size(converted['bytes'])}")
        self.stdout.write(f"Postgres {manifest['postgres']['version']} · commit {manifest['git_commit'] or '(sin git)'}")
        self.stdout.write(self.style.WARNING("Contiene datos personales de inquilinos: compártelo solo por un link "
                                             "privado y nunca lo subas al repo."))


def git_commit() -> str | None:
    """GIT_COMMIT del entorno, o git rev-parse si git y el repo están disponibles (no lo están en el contenedor)."""
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
