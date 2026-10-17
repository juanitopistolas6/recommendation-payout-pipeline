"""Rutas de los documentos. En la BD se guardan relativas a settings.DATA_ROOT, con separador "/":

    claims/123/lease.docx            (original, solo lectura)
    _converted/123/lease.docx.pdf    (conversión, derivada de la ruta del original)

Así la BD sirve en cualquier máquina: solo cambia DATA_ROOT. Nadie concatena rutas a mano: para abrir un
documento se usa resolve_data_path(), y para guardar una ruta, to_relative().
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from django.conf import settings

CLAIMS_DIR = "claims"
CONVERTED_DIR = "_converted"


def data_root() -> Path:
    return Path(settings.DATA_ROOT)


def resolve_data_path(relative_path: str) -> Path:
    """Ruta relativa guardada en la BD -> ruta absoluta en esta máquina.

    Rechaza rutas absolutas y las que salen de DATA_ROOT con "..": una ruta así en la BD es un error de datos.
    """
    if not relative_path:
        raise ValueError("ruta vacía")
    posix = PurePosixPath(relative_path)
    if posix.is_absolute() or "\\" in relative_path or ".." in posix.parts or ":" in posix.parts[0]:
        raise ValueError(f"la ruta debe ser relativa a DATA_ROOT, con '/': {relative_path!r}")
    return data_root().joinpath(*posix.parts)


def to_relative(path: Path | str) -> str:
    """Ruta absoluta dentro de DATA_ROOT -> ruta relativa con "/", para guardarla en la BD."""
    try:
        return Path(path).resolve().relative_to(data_root().resolve()).as_posix()
    except ValueError:
        raise ValueError(f"{path} no está dentro de DATA_ROOT ({settings.DATA_ROOT})") from None


def claims_dir() -> Path:
    return data_root() / CLAIMS_DIR


def converted_dir() -> Path:
    return data_root() / CONVERTED_DIR


def converted_relative_path(original: str, suffix: str) -> str:
    """Dónde va la conversión de un original: claims/123/sub/Lease.docx -> _converted/123/sub/Lease.docx.pdf.

    Se conserva la extensión original para que Lease.docx y Lease.xlsx de la misma carpeta no choquen.
    """
    parts = PurePosixPath(original).parts
    if len(parts) < 3 or parts[0] != CLAIMS_DIR:
        raise ValueError(f"el original debe estar en {CLAIMS_DIR}/<tracking_id>/: {original!r}")
    return PurePosixPath(CONVERTED_DIR, *parts[1:-1], parts[-1] + suffix).as_posix()
