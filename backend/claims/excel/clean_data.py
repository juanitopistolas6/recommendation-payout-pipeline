"""Lectura y limpieza del Excel de claims.

Funciones puras (sin Django) para poder probarlas por separado:
    rows = read_claims_file(path)
    for row in rows:
        parsed = parse_row(row)   # -> ParsedRow con claim, outcome, raw y warnings
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import pandas as pd


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

EMPTY_VALUES = {"", "nan", "nat", "none", "null", "n/a", "na", "-", "--"}


def _clean(value: Any) -> str:
    """Convierte cualquier valor de celda a texto limpio ('' si está vacío)."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in EMPTY_VALUES else text


def parse_text(value: Any) -> str:
    return _clean(value)


def parse_id(value: Any) -> str:
    """Tracking number: '1', 1, 1.0 y '1.0' -> '1'. Conserva IDs alfanuméricos."""
    text = _clean(value)
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    return text


def parse_money(value: Any) -> Decimal | None:
    """'$2,500', '$1.00', '2500', '(100.00)', '-$50' -> Decimal con 2 decimales."""
    text = _clean(value)
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    digits = re.sub(r"[^0-9.]", "", text)
    if not digits or digits.count(".") > 1:
        raise ValueError(f"monto no reconocido: {value!r}")
    try:
        amount = Decimal(digits).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"monto no reconocido: {value!r}") from exc
    return -amount if negative and amount else amount  # evita '-0.00'


DATE_FORMATS = (
    "%m/%d/%y",            # 03/04/22  (formato del Excel original)
    "%m/%d/%Y",            # 03/04/2022
    "%Y-%m-%d",            # 2022-03-04
    "%Y-%m-%d %H:%M:%S",   # celdas de tipo fecha leídas como texto
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %H:%M",
)
EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    # Número serial de Excel (ej. 44624 = 2022-03-04)
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        return EXCEL_EPOCH + timedelta(days=int(float(text)))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no reconocida: {value!r}")


TRUE_VALUES = {"yes", "y", "true", "t", "1", "si", "sí", "x", "audit"}
FALSE_VALUES = {"no", "n", "false", "f", "0"}


def parse_bool(value: Any) -> bool | None:
    text = _clean(value).lower()
    if not text:
        return None
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"booleano no reconocido: {value!r}")


# ---------------------------------------------------------------------------
# Mapeo de columnas
# ---------------------------------------------------------------------------

Parser = Callable[[Any], Any]

# Columnas que el pipeline PUEDE usar -> modelo Claim
CLAIM_COLUMNS: dict[str, tuple[str, Parser]] = {
    "Tracking Number": ("tracking_id", parse_id),
    "Claim Date": ("claim_date", parse_date),
    "Lease Street Address": ("lease_street_address", parse_text),
    "Lease City": ("lease_city", parse_text),
    "Lease State": ("lease_state", parse_text),
    "Lease Zip": ("lease_zip", parse_text),
    "Lease Start Date": ("lease_start_date", parse_date),
    "Lease End Date": ("lease_end_date", parse_date),
    "Move-Out Date": ("move_out_date", parse_date),
    "Monthly Rent": ("monthly_rent", parse_money),
    "Amount of Claim": ("claim_amount", parse_money),
    "Termination Type": ("termination_type", parse_text),
    "Property Management Company": ("property_management_company", parse_text),
    "Property Manager Name": ("property_manager_name", parse_text),
    "Group #": ("group_number", parse_text),
    "Treaty #": ("treaty_number", parse_text),
    "Policy": ("policy", parse_text),
    "Max Benefit": ("max_benefit", parse_money),
    "PM Explanation": ("pm_explanation", parse_text),
}

# Columnas que se llenan DESPUÉS de la decisión humana -> modelo ClaimOutcome.
# Se excluyen de Claim.raw_row para que nunca lleguen a un prompt.
OUTCOME_COLUMNS: dict[str, tuple[str, Parser]] = {
    "Approved Benefit Amount": ("approved_benefit", parse_money),
    "Status": ("status", parse_text),
    "Approval Date": ("approval_date", parse_date),
    "Posted Date": ("posted_date", parse_date),
    "Pending Docs from PM": ("pending_docs_from_pm", parse_text),
    "Hold Reason": ("hold_reason", parse_text),
    "Tenant Contacted": ("tenant_contacted", parse_text),
    "Tenant Collection Status": ("tenant_collection_status", parse_text),
    "Collection Status": ("collection_status", parse_text),
    "Send to Collections": ("send_to_collections", parse_bool),
    "Agreed Tenant Settlement": ("agreed_tenant_settlement", parse_money),
    "Agreed Tenant Settlement Date": ("agreed_tenant_settlement_date", parse_date),
    "Collected Date": ("collected_date", parse_date),
    "Collected Amount": ("collected_amount", parse_money),
    "Collection Processed Date": ("collection_processed_date", parse_date),
    "Audit Selection": ("audit_selection", parse_bool),
}

# Columnas de acciones de la interfaz original. No son datos del claim y pueden
# reflejar el flujo posterior a la decisión: van solo a ClaimOutcome.raw_outcome.
OUTCOME_RAW_ONLY = {
    "Review Claim Adjudication",
    "Review Tenant Information",
    "Update YRIG Policy Info",
    "Open Collections",
    "View PM Information",
    "PM Notification of Claim Received",
}

REQUIRED_COLUMNS = {"Tracking Number", "Amount of Claim", "Max Benefit", "Approved Benefit Amount"}


def normalize_header(name: Any) -> str:
    """'  Move-Out   Date ' -> 'move-out date'. Hace el match tolerante a espacios y mayúsculas."""
    return re.sub(r"\s+", " ", str(name)).strip().lower()


_CLAIM_BY_KEY = {normalize_header(k): (k, v) for k, v in CLAIM_COLUMNS.items()}
_OUTCOME_BY_KEY = {normalize_header(k): (k, v) for k, v in OUTCOME_COLUMNS.items()}
_OUTCOME_RAW_KEYS = {normalize_header(k) for k in OUTCOME_RAW_ONLY}


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

def read_claims_file(path: str | Path, sheet: str | int = 0) -> pd.DataFrame:
    """Lee .xlsx/.xls/.csv con TODO como texto, para parsear cada columna a mano.

    Leer como texto evita que pandas convierta '00123' en 123 o un ID en 1.0.
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(path, sheet_name=sheet, dtype=str, keep_default_na=False)

    # Quita columnas sin encabezado (en el original hay una vacía tras "Approved Benefit Amount")
    df = df.loc[:, [not str(c).startswith("Unnamed:") and str(c).strip() != "" for c in df.columns]]
    # Quita filas completamente vacías
    df = df[df.apply(lambda r: any(_clean(v) for v in r), axis=1)]
    return df


def check_columns(columns: list[str]) -> tuple[list[str], list[str]]:
    """Devuelve (faltantes requeridas, columnas no reconocidas)."""
    present = {normalize_header(c) for c in columns}
    missing = sorted(c for c in REQUIRED_COLUMNS if normalize_header(c) not in present)
    known = set(_CLAIM_BY_KEY) | set(_OUTCOME_BY_KEY) | _OUTCOME_RAW_KEYS
    unknown = sorted(c for c in columns if normalize_header(c) not in known)
    return missing, unknown


# ---------------------------------------------------------------------------
# Parseo de una fila
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    claim: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    raw_row: dict[str, str] = field(default_factory=dict)      # -> Claim.raw_row
    raw_outcome: dict[str, str] = field(default_factory=dict)  # -> ClaimOutcome.raw_outcome
    warnings: list[str] = field(default_factory=list)


def parse_row(row: pd.Series) -> ParsedRow:
    parsed = ParsedRow()

    for column, value in row.items():
        key = normalize_header(column)
        original = _clean(value)

        if key in _OUTCOME_BY_KEY:
            parsed.raw_outcome[str(column)] = original
            target, parser = _OUTCOME_BY_KEY[key][1]
            _assign(parsed.outcome, target, parser, value, column, parsed.warnings)
        elif key in _OUTCOME_RAW_KEYS:
            parsed.raw_outcome[str(column)] = original
        else:
            # Todo lo que no es resultado va a raw_row, incluidas columnas no mapeadas
            parsed.raw_row[str(column)] = original
            if key in _CLAIM_BY_KEY:
                target, parser = _CLAIM_BY_KEY[key][1]
                _assign(parsed.claim, target, parser, value, column, parsed.warnings)

    return parsed


def _assign(dest: dict, target: str, parser: Parser, value: Any, column: str, warnings: list[str]) -> None:
    try:
        dest[target] = parser(value)
    except ValueError as exc:
        # Un valor raro no tumba la fila: se guarda vacío y queda el original en raw_*
        dest[target] = "" if parser in (parse_text, parse_id) else None
        warnings.append(f"{column}: {exc}")