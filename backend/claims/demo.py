"""Paquete demo: la BD procesada + data/_converted/ en un ZIP, para ver los resultados sin registrar documentos
ni llamar a Gemini. Lo usan export_demo e import_demo.

    claims-demo.zip
    ├── manifest.json
    ├── db.dump          pg_dump -Fc de toda la BD (sin datos de sesiones)
    └── _converted/      copia de DATA_ROOT/_converted/

Contiene datos personales de inquilinos (full_text y archivos convertidos): nunca se sube al repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from django.db import connection

from claims import paths
from claims.models import Claim, ClaimDocument, ClaimExtraction, LeaseExtraction, LLMCall, Recommendation

MANIFEST = "manifest.json"
DUMP = "db.dump"
FORMAT_VERSION = 1

COUNTED_MODELS = {
    "claims": Claim,
    "claim_documents": ClaimDocument,
    "lease_extractions": LeaseExtraction,
    "claim_extractions": ClaimExtraction,
    "recommendations": Recommendation,
    "llm_calls": LLMCall,
}


class DemoError(Exception):
    pass


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

def pg_env() -> dict:
    """Variables de entorno de libpq con las credenciales de settings (la contraseña no va en la línea de comandos)."""
    db = connection.settings_dict  # en los tests apunta a la BD de pruebas
    env = {**os.environ, "PGHOST": str(db["HOST"]), "PGPORT": str(db["PORT"]), "PGUSER": db["USER"],
           "PGDATABASE": db["NAME"]}
    if db.get("PASSWORD"):
        env["PGPASSWORD"] = db["PASSWORD"]
    return env


def server_version() -> dict:
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        version = cursor.fetchone()[0].split()[0]  # "18.6" o "18.6 (Debian ...)"
        cursor.execute("SHOW server_version_num")
        number = int(cursor.fetchone()[0])
    return {"version": version, "major": number // 10000}


def tool_major(tool: str) -> int:
    """Versión mayor de pg_dump / pg_restore ("pg_dump (PostgreSQL) 18.6 (Debian ...)" -> 18)."""
    try:
        out = subprocess.run([tool, "--version"], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise DemoError(f"{tool} no está instalado (postgresql-client en el Dockerfile del backend)") from None
    match = re.search(r"\)\s+(\d+)", out)
    if not match:
        raise DemoError(f"No se pudo leer la versión de {tool}: {out.strip()}")
    return int(match.group(1))


def check_tool(tool: str, server_major: int) -> None:
    major = tool_major(tool)
    if major < server_major:
        raise DemoError(f"{tool} {major} es más viejo que el servidor Postgres {server_major}: "
                        f"instala postgresql-client-{server_major} en el Dockerfile del backend")


def run_pg(args: list[str]) -> None:
    result = subprocess.run(args, env=pg_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise DemoError(f"{args[0]} falló (código {result.returncode}):\n{result.stderr.strip()[-2000:]}")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def counts() -> dict[str, int]:
    return {name: model.objects.count() for name, model in COUNTED_MODELS.items()}


def natural_key(tracking: str):
    return (len(tracking), tracking)


def recommended_tracking_numbers() -> list[str]:
    return sorted(set(Recommendation.objects.values_list("claim_id", flat=True)), key=natural_key)


def versions() -> dict:
    def distinct(model, field):
        return sorted(set(model.objects.values_list(field, flat=True)))

    return {
        "rules_versions": distinct(Recommendation, "rules_version"),
        "prompt_versions": {
            "lease": distinct(LeaseExtraction, "prompt_version"),
            "claim": distinct(ClaimExtraction, "prompt_version"),
        },
    }


def converted_files() -> list[Path]:
    root = paths.converted_dir()
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
