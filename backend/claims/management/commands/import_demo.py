"""Carga el paquete demo de export_demo en esta instalación: restaura la BD y extrae _converted/.

Uso:
    docker compose exec backend python manage.py import_demo /data/demo/claims-demo.zip
    docker compose exec backend python manage.py import_demo /data/demo/claims-demo.zip --force

Antes de tocar nada valida el ZIP, la versión de Postgres y que la BD no tenga claims (salvo --force).
pg_restore corre en una sola transacción: si falla, la BD queda como estaba. Después aplica las migraciones
que el dump no tenga y compara los conteos con los del manifest.

Los documentos originales no vienen en el paquete: los resultados ya procesados se ven igual, pero para
reprocesar o analizar claims nuevos hay que descargarlos en data/claims/.
"""

import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import DatabaseError

from claims import demo, paths
from claims.models import Claim, ClaimDocument, Recommendation
from claims.pipeline.rules import RULES_VERSION


class Command(BaseCommand):
    help = "Restaura el paquete demo (manifest.json + db.dump + _converted/) generado por export_demo."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Ruta del ZIP dentro del contenedor, ej. /data/demo/claims-demo.zip")
        parser.add_argument("--force", action="store_true",
                            help="Reemplaza la BD aunque ya tenga claims")

    def handle(self, *args, **opts):
        try:
            self._import(Path(opts["path"]), opts["force"])
        except demo.DemoError as exc:
            raise CommandError(str(exc)) from None

    def _import(self, path: Path, force: bool) -> None:
        # 1. Validaciones: nada se toca hasta que todas pasan
        manifest = read_manifest(path)
        server = demo.server_version()
        expected = manifest["postgres"]
        if server["major"] < expected["major"]:
            raise demo.DemoError(f"El paquete viene de Postgres {expected['version']} y este servidor es "
                                 f"{server['version']}: pg_restore no carga un dump en un servidor más viejo")
        demo.check_tool("pg_restore", max(server["major"], expected.get("pg_dump", expected["major"])))
        existing = existing_data()
        if existing["claims"] and not force:
            raise demo.DemoError(
                f"La base de datos ya tiene {existing['claims']} claims ({existing['recommended']} con recomendaciones). "
                "import_demo la reemplazaría completa (claims, documentos registrados, extracciones, recomendaciones "
                "y llamadas al LLM). No se cambió nada.\n"
                "Si quieres reemplazarla, vuelve a correrlo con --force.")

        # 2. Restauración
        with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / demo.DUMP
            with zf.open(demo.DUMP) as src, dump.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            self.stdout.write("Restaurando la base de datos (pg_restore)...")
            self.stdout.flush()
            connection.close()  # el esquema se borra y se recrea: ninguna conexión de Django debe quedar en medio
            demo.run_pg(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
                         "--single-transaction", "-d", connection.settings_dict["NAME"], str(dump)])
            connection.close()

            self.stdout.write("Aplicando migraciones pendientes...")
            call_command("migrate", interactive=False, verbosity=0)

            extracted = extract_converted(zf)

        # 3. Verificación
        self._verify(manifest, extracted, existing)

    def _verify(self, manifest: dict, extracted: int, replaced: dict) -> None:
        actual = demo.counts()
        expected = manifest["counts"]
        width = max(map(len, expected))
        self.stdout.write(self.style.SUCCESS(f"\nPaquete importado (exportado el {manifest['exported_at']}"
                                             f"{', commit ' + manifest['git_commit'] if manifest.get('git_commit') else ''})"))
        if replaced["claims"]:
            self.stdout.write(self.style.WARNING(f"Se reemplazó una base que tenía {replaced['claims']} claims "
                                                 f"({replaced['recommended']} con recomendaciones)."))
        mismatches = []
        for name, value in expected.items():
            ok = actual.get(name) == value
            if not ok:
                mismatches.append(name)
            self.stdout.write(f"  {name:<{width}}  {actual.get(name, 0):>6}  "
                              + ("ok" if ok else self.style.ERROR(f"el manifest dice {value}")))
        self.stdout.write(f"_converted/: {extracted} archivos extraídos en {paths.converted_dir()}")

        with_results = len(demo.recommended_tracking_numbers())
        self.stdout.write(f"Claims con resultados: {with_results}")
        rules_versions = manifest.get("rules_versions", [])
        if rules_versions and RULES_VERSION not in rules_versions:
            self.stdout.write(self.style.WARNING(
                f"Las recomendaciones son de rules_version {', '.join(rules_versions)} y el código usa {RULES_VERSION}: "
                f"las métricas por defecto salen vacías; usa ?rules_version={rules_versions[-1]} o "
                "recompute_recommendations --all"))

        docs = list(ClaimDocument.objects.only("file_path", "converted_pdf_path"))
        missing = sum(not paths.resolve_data_path(d.file_path).is_file() for d in docs)
        missing_converted = sum(bool(d.converted_pdf_path) and not paths.resolve_data_path(d.converted_pdf_path).is_file()
                                for d in docs)
        self.stdout.write(f"Documentos originales en disco: {len(docs) - missing} de {len(docs)}")

        self.stdout.write("\nQué falta para procesar claims nuevos:")
        pending = []
        if missing:
            pending.append(f"Faltan {missing} archivos originales; descarga la carpeta de documentos en data/claims "
                           "para reprocesar o analizar claims nuevos.")
        if missing_converted:
            pending.append(f"Faltan {missing_converted} archivos convertidos en _converted/: register_documents "
                           "los vuelve a generar si están los originales.")
        pending.append("Para un claim que nunca se registró: register_documents --ids <tracking> antes de analizarlo.")
        pending.append("GEMINI_API_KEY en .env (los resultados importados se ven sin ella).")
        for line in pending:
            style = self.style.WARNING if line.startswith("Faltan") else (lambda s: s)
            self.stdout.write(style(f"  - {line}"))

        if mismatches:
            raise CommandError(f"Los conteos no coinciden con el manifest: {', '.join(mismatches)}")


def read_manifest(path: Path) -> dict:
    if not path.is_file():
        raise demo.DemoError(f"No existe el archivo {path}")
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            missing = [n for n in (demo.MANIFEST, demo.DUMP) if n not in names]
            if missing:
                raise demo.DemoError(f"{path.name} no es un paquete demo: le falta {', '.join(missing)}")
            unsafe = [n for n in names if n.startswith("/") or "\\" in n or ".." in PurePosixPath(n).parts]
            if unsafe:
                raise demo.DemoError(f"Rutas no permitidas en el ZIP: {', '.join(sorted(unsafe)[:5])}")
            manifest = json.loads(zf.read(demo.MANIFEST))
    except zipfile.BadZipFile:
        raise demo.DemoError(f"{path.name} no es un ZIP válido") from None
    except json.JSONDecodeError as exc:
        raise demo.DemoError(f"manifest.json no es JSON válido: {exc}") from None
    if manifest.get("format_version") != demo.FORMAT_VERSION:
        raise demo.DemoError(f"Formato de paquete {manifest.get('format_version')!r} no soportado "
                             f"(este código lee el {demo.FORMAT_VERSION})")
    for key in ("postgres", "counts", "exported_at"):
        if key not in manifest:
            raise demo.DemoError(f"manifest.json no tiene '{key}'")
    return manifest


def existing_data() -> dict:
    """Claims y claims con recomendación en la BD actual (0 si las tablas todavía no existen).

    Se protege cualquier claim, no solo los que tienen resultados: el paquete puede venir sin recomendaciones,
    y reemplazar una BD con documentos ya registrados también pierde trabajo."""
    try:
        return {"claims": Claim.objects.count(),
                "recommended": Recommendation.objects.values("claim_id").distinct().count()}
    except DatabaseError:
        connection.close()  # una consulta fallida deja la conexión en estado de error
        return {"claims": 0, "recommended": 0}


def extract_converted(zf: zipfile.ZipFile) -> int:
    """Extrae _converted/ en DATA_ROOT/_converted/, sobrescribiendo los archivos con el mismo nombre.
    Las rutas del ZIP ya se validaron en read_manifest (nada absoluto ni con "..")."""
    count = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(paths.CONVERTED_DIR + "/"):
            continue
        target = paths.resolve_data_path(info.filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count
