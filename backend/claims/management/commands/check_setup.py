"""Verifica que todo esté listo para pedir recomendaciones al LLM, sin gastar cuota.

Uso:
    docker compose exec backend python manage.py check_setup
    docker compose exec backend python manage.py check_setup --ids 1,10,100   # solo los archivos de esos claims
    docker compose exec backend python manage.py check_setup --live           # + 1 llamada real a Gemini

Revisa: Gemini (key, modo fake, SDK), modelos y cuota del día, prompts, base de datos (claims y documentos),
archivos en DATA_ROOT, y Redis + worker. Cada punto sale como OK, AVISO o ERROR con cómo arreglarlo.
Termina con error si algún punto falla; los avisos no lo hacen fallar.
"""

from __future__ import annotations

from pathlib import Path

import django_rq
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from rq import Worker

from claims.jobs import QUEUE_NAME
from claims.llm.client import PROMPTS_DIR, get_client
from claims.llm.rate_limit import calls_today
from claims.models import Claim, ClaimDocument
from claims.paths import claims_dir, converted_dir, data_root

PROMPTS = ("classify", "lease", "claim")
MISSING_EXAMPLES = 5  # cuántas rutas faltantes se muestran de ejemplo
PLACEHOLDER_KEY = "your-gemini-api-key"

OK, WARN, FAIL = "ok", "warn", "fail"


class Command(BaseCommand):
    help = "Verifica la configuración necesaria para procesar claims con Gemini, sin llamar a la API."

    def add_arguments(self, parser):
        parser.add_argument("--ids", help="Tracking IDs separados por coma: revisa solo los archivos de esos claims")
        parser.add_argument("--live", action="store_true",
                            help="Al final hace 1 llamada real a Gemini (llm_smoke_test) con un documento")

    def handle(self, *args, **opts):
        self.failures = 0
        self.warnings = 0
        ids = [i.strip() for i in opts["ids"].split(",") if i.strip()] if opts["ids"] else None

        self.section("Gemini")
        self.check_gemini()
        self.section("Modelos y cuota del día")
        self.check_models()
        self.section("Prompts")
        self.check_prompts()
        self.section("Base de datos")
        db_ok = self.check_database(ids)
        self.section("Archivos")
        sample = self.check_files(ids) if db_ok else None
        self.section("Cola (Redis + worker)")
        self.check_queue()

        if opts["live"]:
            self.section("Llamada real (--live)")
            self.check_live(sample)

        self.stdout.write("")
        summary = f"{self.failures} error(es), {self.warnings} aviso(s)"
        if self.failures:
            raise CommandError(f"La configuración no está lista: {summary}")
        self.stdout.write(self.style.SUCCESS(f"Todo listo para pedir recomendaciones ({summary})"))

    # -- Salida -------------------------------------------------------------

    def section(self, title):
        self.stdout.write(f"\n{title}")

    def report(self, level, message, fix=""):
        if level == FAIL:
            self.failures += 1
            line = self.style.ERROR(f"  [ERROR] {message}")
        elif level == WARN:
            self.warnings += 1
            line = self.style.WARNING(f"  [AVISO] {message}")
        else:
            line = self.style.SUCCESS("  [OK]    ") + message
        self.stdout.write(line)
        if fix and level != OK:
            self.stdout.write(f"          -> {fix}")

    # -- Verificaciones -----------------------------------------------------

    def check_gemini(self):
        if settings.GEMINI_FAKE:
            self.report(WARN, "GEMINI_FAKE=1: el pipeline usa respuestas fijas y no llama a Gemini",
                        "Pon GEMINI_FAKE=0 en .env y reinicia: docker compose up -d backend worker")
        else:
            self.report(OK, "GEMINI_FAKE=0: se llama a la API real")

        key = settings.GEMINI_API_KEY
        if not key or key == PLACEHOLDER_KEY:
            level = WARN if settings.GEMINI_FAKE else FAIL
            self.report(level, "Falta GEMINI_API_KEY",
                        "Ponla en .env y reinicia: docker compose up -d backend worker")
        else:
            self.report(OK, f"GEMINI_API_KEY configurada (...{key[-4:]})")

        try:
            from google import genai  # noqa: F401
        except ImportError:
            self.report(FAIL, "No se pudo importar el SDK google-genai",
                        "Reconstruye la imagen: docker compose build backend worker")
        else:
            self.report(OK, "SDK google-genai instalado")

    def check_models(self):
        client = get_client()
        roles = (
            ("classify", settings.GEMINI_MODEL_CLASSIFY),
            ("extract", settings.GEMINI_MODEL_EXTRACT),
            ("retry", settings.GEMINI_MODEL_RETRY),
        )
        for role, model in roles:
            limits = settings.GEMINI_RATE_LIMITS.get(model)
            if not limits:
                self.report(FAIL, f"{role}: {model} no tiene límites en GEMINI_RATE_LIMITS",
                            "Agrégalo en backend/config/settings.py o cambia GEMINI_MODEL_" + role.upper())
                continue
            try:
                used = calls_today(client.quota_model_name(model))
            except Exception as exc:  # sin base de datos no se puede contar; ya lo reporta check_database
                self.report(WARN, f"{role}: {model} — no se pudo contar la cuota ({type(exc).__name__})")
                continue
            cutoff = int(limits["rpd"] * settings.GEMINI_DAILY_QUOTA_THRESHOLD)
            message = f"{role}: {model} — usadas hoy {used}/{limits['rpd']} (corte en {cutoff}), {limits['rpm']} RPM"
            if used >= cutoff:
                # retry solo se usa en casos difíciles: sin cuota no bloquea, esos claims quedan para revisión
                level = WARN if role == "retry" else FAIL
                self.report(level, message + " — cuota agotada",
                            "La cuota se reinicia a medianoche del Pacífico (America/Los_Angeles)")
            elif used >= cutoff * 0.8:
                self.report(WARN, message + " — queda poca cuota")
            else:
                self.report(OK, message)

    def check_prompts(self):
        for name in PROMPTS:
            path = PROMPTS_DIR / f"{name}.md"
            if not path.is_file():
                self.report(FAIL, f"Falta el prompt {name}.md", f"Debe estar en {path}")
            elif not path.read_text(encoding="utf-8").strip():
                self.report(FAIL, f"El prompt {name}.md está vacío", f"Revisa {path}")
            else:
                self.report(OK, f"{name}.md")

    def check_database(self, ids) -> bool:
        try:
            connection.ensure_connection()
        except Exception as exc:
            self.report(FAIL, f"No hay conexión con Postgres: {exc}", "Levanta la base: docker compose up -d db")
            return False

        try:
            claims = Claim.objects.all()
            if ids:
                missing = set(ids) - set(claims.filter(pk__in=ids).values_list("pk", flat=True))
                if missing:
                    self.report(FAIL, f"No existen los claims: {', '.join(sorted(missing))}")
                claims = claims.filter(pk__in=ids)
            claim_count = claims.count()
        except Exception as exc:
            self.report(FAIL, f"No se pudo leer la tabla de claims: {exc}",
                        "Aplica las migraciones: docker compose exec backend python manage.py migrate")
            return False

        if not claim_count:
            self.report(FAIL, "No hay claims",
                        "Importa el paquete demo (import_demo) o el Excel (import_claims)")
            return False
        self.report(OK, f"{claim_count} claims")

        doc_count = ClaimDocument.objects.filter(claim__in=claims).count()
        if not doc_count:
            self.report(FAIL, "No hay documentos registrados",
                        "Registra los archivos: docker compose exec backend python manage.py register_documents --all")
            return False
        self.report(OK, f"{doc_count} documentos registrados")

        without_docs = claims.filter(documents__isnull=True).count()
        if without_docs:
            self.report(WARN, f"{without_docs} claims sin documentos: no se pueden procesar",
                        "Si deberían tener archivos, revisa data/claims/<tracking_id>/ y corre register_documents")
        return True

    def check_files(self, ids) -> ClaimDocument | None:
        """Comprueba que exista el archivo que se manda a Gemini de cada documento. Devuelve uno que sí existe
        (preferiblemente PDF) para la llamada --live."""
        root = data_root()
        if not root.is_dir():
            self.report(FAIL, f"No existe DATA_ROOT ({root})", "Revisa DATA_ROOT en .env y el volumen ./data:/data")
            return None
        self.report(OK, f"DATA_ROOT: {root}")

        if not claims_dir().is_dir():
            self.report(FAIL, f"No existe {claims_dir()}",
                        "Descarga los documentos de los claims a data/claims/<tracking_id>/ (ver README)")
            return None

        documents = ClaimDocument.objects.order_by("claim_id", "file_name")
        if ids:
            documents = documents.filter(claim_id__in=ids)

        missing_original, missing_converted, sample, checked = [], [], None, 0
        for doc in documents.only("claim_id", "file_path", "file_name", "converted_pdf_path", "mime_type").iterator():
            checked += 1
            try:
                path, mime = doc.llm_source
            except ValueError:
                missing_original.append(doc.file_path)
                continue
            if Path(path).is_file():
                if sample is None or (mime == "application/pdf" and sample.llm_source[1] != "application/pdf"):
                    sample = doc
            elif doc.converted_pdf_path:
                missing_converted.append(doc.converted_pdf_path)
            else:
                missing_original.append(doc.file_path)

        if missing_original:
            self.report(FAIL, f"Faltan {len(missing_original)} de {checked} archivos originales, ej.: "
                        + ", ".join(missing_original[:MISSING_EXAMPLES]),
                        "Descarga los documentos a data/claims/ conservando data/claims/<tracking_id>/...")
        if missing_converted:
            self.report(FAIL, f"Faltan {len(missing_converted)} archivos convertidos en {converted_dir()}, ej.: "
                        + ", ".join(missing_converted[:MISSING_EXAMPLES]),
                        "Vuelve a importar el paquete demo (import_demo --force) o corre register_documents")
        if not missing_original and not missing_converted:
            self.report(OK, f"Los {checked} archivos que se mandan a Gemini existen")
        return sample

    def check_queue(self):
        try:
            conn = django_rq.get_connection(QUEUE_NAME)
            conn.ping()
        except Exception as exc:
            self.report(FAIL, f"Redis no responde: {exc}", "Levántalo: docker compose up -d redis")
            return
        self.report(OK, "Redis responde")

        queue = django_rq.get_queue(QUEUE_NAME)
        workers = Worker.all(connection=conn, queue=queue)
        if not workers:
            self.report(FAIL, f"No hay worker escuchando la cola '{QUEUE_NAME}': los claims se quedan en cola",
                        "Levántalo: docker compose up -d worker (o usa process_claims --sync)")
        else:
            self.report(OK, f"{len(workers)} worker(s) en la cola '{QUEUE_NAME}', {queue.count} job(s) esperando")

    def check_live(self, sample):
        if self.failures:
            self.report(WARN, "Se omite: primero corrige los errores de arriba")
            return
        if settings.GEMINI_FAKE:
            self.report(WARN, "Se omite: GEMINI_FAKE=1")
            return
        if sample is None:
            self.report(WARN, "Se omite: no hay ningún documento disponible para mandar")
            return

        path = sample.llm_source[0]
        self.stdout.write(f"  Documento: {path}")
        try:
            call_command("llm_smoke_test", path, stdout=self.stdout, stderr=self.stderr)
        except CommandError as exc:
            self.report(FAIL, f"La llamada falló: {exc}", "Revisa GEMINI_API_KEY y el detalle del LLMCall de arriba")
        else:
            self.report(OK, "Gemini respondió y la respuesta validó")
