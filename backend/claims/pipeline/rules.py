"""Motor de reglas: de la extracción del LLM al monto recomendado, los flags y la justificación.

El LLM lee, clasifica y juzga. Aquí el código suma, valida y aplica el tope.

Estructura:
    - Núcleo puro (evaluate): dataclasses + Decimal, sin LLM y sin BD. Se recalcula al instante.
    - Adaptadores (extraction_from_json, lease_from_json): traducen el JSON guardado de Prompt B / Prompt A
      al núcleo. Es lo único que depende de los nombres de campo del esquema del apéndice.
    - build_recommendation: junta los datos del claim y devuelve un Recommendation sin guardar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

RULES_VERSION = "v1"

CATEGORY_OVERRIDES = {
    "program_fee": False,
    "normal_wear_and_tear": False,
    "convenience_fee": None,   # None = respetar el juicio del LLM
    "late_fee": None,
    "legal_fee": None,
    "admin_fee": None,
}

PAYMENT_TYPES = {"payment", "credit", "concession"}
CRITICAL_FLAGS = {"totals_mismatch", "illegible_document", "photos_only", "missing_documentation", "missing_max_benefit"}

TOTALS_TOLERANCE = Decimal("1.00")
MIN_CLAIM_AMOUNT = Decimal("10.00")
CLAIM_AMOUNT_TOLERANCE = Decimal("0.20")
MIN_TEXT_CHARS = 100  # mismo umbral que la clasificación: menos que esto es un escaneo sin texto
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# Datos de entrada del núcleo
# ---------------------------------------------------------------------------

@dataclass
class Charge:
    description: str
    amount: Decimal
    date: date | None = None
    category: str | None = None
    eligible: bool | None = None           # juicio del LLM
    reason: str = ""                       # razón del LLM
    source_document: str | None = None     # file_name del documento de donde salió
    eligibility_basis: str | None = None   # lease | program_default
    lease_reference: str | None = None     # sección del contrato que lo respalda
    prorated: bool = False                 # renta parcial: no se compara contra monthly_rent


@dataclass
class Payment:
    type: str
    amount: Decimal
    date: date | None = None
    description: str = ""
    source_document: str | None = None


@dataclass
class Extraction:
    charges: list[Charge] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    deposit_held: Decimal = ZERO
    printed_total: Decimal | None = None
    confidence: str = ""
    flags: list[str] = field(default_factory=list)  # flags que puso el LLM (illegible_document, ...)


@dataclass
class FeeTerm:
    kind: str          # fixed | percent_of_rent | per_unit | per_day
    value: Decimal


@dataclass
class LeaseTerms:
    coverage_amount: Decimal | None = None
    # Por categoría (late_fee, admin_fee, ...). Puede haber varios: p. ej. late fee $50 + $10 por día
    fees: dict[str, list[FeeTerm]] = field(default_factory=dict)


@dataclass
class ClaimData:
    max_benefit: Decimal | None
    claim_amount: Decimal | None
    monthly_rent: Decimal | None


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class ChargeResult:
    charge: Charge
    eligible: bool
    eligibility_source: str   # override | llm | default
    reason: str
    paid: Decimal
    balance: Decimal

    def as_json(self) -> dict:
        c = self.charge
        return {
            "description": c.description,
            "date": c.date.isoformat() if c.date else None,
            "category": c.category,
            "amount": str(c.amount),
            "eligible": self.eligible,
            "eligibility_source": self.eligibility_source,
            "eligibility_basis": c.eligibility_basis,
            "lease_reference": c.lease_reference,
            "reason": self.reason,
            "paid": str(self.paid),
            "balance": str(self.balance),
            "source_document": c.source_document,
        }


@dataclass
class RuleResult:
    recommended_amount: Decimal
    eligible_owed: Decimal
    excluded_total: Decimal
    cap_applied: bool
    flags: list[str]
    route: str
    charges: list[ChargeResult]
    explanation: str
    charges_total: Decimal
    payments_total: Decimal


# ---------------------------------------------------------------------------
# Núcleo
# ---------------------------------------------------------------------------

def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def decide_eligibility(charge: Charge, overrides: dict = CATEGORY_OVERRIDES) -> tuple[bool, str, str]:
    """(elegible, fuente, razón). La sobrescritura por categoría gana; None respeta al LLM."""
    override = overrides.get(charge.category or "")
    if override is not None:
        verb = "always" if override else "never"
        return override, "override", f"Rule {RULES_VERSION}: {charge.category} is {verb} eligible"
    if charge.eligible is None:
        return False, "default", "The LLM did not judge eligibility; excluded by default"
    return charge.eligible, "llm", charge.reason


def apply_fifo(charges: list[Charge], payments: list[Payment]) -> list[tuple[Charge, Decimal, Decimal]]:
    """Aplica pagos, créditos y concesiones a los cargos del más antiguo al más reciente.

    Los cargos sin fecha van al final; con la misma fecha se respeta el orden del documento.
    Devuelve (cargo, pagado, saldo) en el orden original de los cargos.
    """
    pool = sum((p.amount for p in payments if p.type in PAYMENT_TYPES), ZERO)
    order = sorted(range(len(charges)), key=lambda i: (charges[i].date is None, charges[i].date or date.min, i))
    paid = [ZERO] * len(charges)
    for i in order:
        if pool <= 0:
            break
        applied = min(pool, max(charges[i].amount, ZERO))
        paid[i] = applied
        pool -= applied
    return [(c, money(paid[i]), money(c.amount - paid[i])) for i, c in enumerate(charges)]


def evaluate(ext: Extraction, lease: LeaseTerms | None, claim: ClaimData,
             doc_texts: dict[str, str] | None = None, overrides: dict = CATEGORY_OVERRIDES) -> RuleResult:
    doc_texts = doc_texts or {}

    results = []
    for charge, paid, balance in apply_fifo(ext.charges, ext.payments):
        eligible, source, reason = decide_eligibility(charge, overrides)
        results.append(ChargeResult(charge, eligible, source, reason, paid, balance))

    charges_total = money(sum((c.amount for c in ext.charges), ZERO))
    payments_total = money(sum((p.amount for p in ext.payments if p.type in PAYMENT_TYPES), ZERO))
    eligible_owed = money(sum((r.balance for r in results if r.eligible), ZERO))
    excluded_total = money(sum((r.balance for r in results if not r.eligible), ZERO))
    owed_after_deposit = eligible_owed - (ext.deposit_held or ZERO)

    flags = list(dict.fromkeys(f for f in ext.flags if f))  # los del LLM, sin repetir
    cap_applied = False
    if claim.max_benefit is None:
        recommended = money(max(ZERO, owed_after_deposit))
        flags.append("missing_max_benefit")
    else:
        cap_applied = owed_after_deposit > claim.max_benefit
        recommended = money(max(ZERO, min(owed_after_deposit, claim.max_benefit)))

    computed = compute_flags(ext, lease, claim, doc_texts, charges_total, payments_total)
    flags += [f for f in computed if f not in flags]

    route = decide_route(ext.confidence, flags)
    explanation = explain(ext, claim, results, charges_total, eligible_owed, excluded_total,
                          recommended, cap_applied, flags)
    return RuleResult(recommended, eligible_owed, excluded_total, cap_applied, flags, route, results,
                      explanation, charges_total, payments_total)


def decide_route(confidence: str, flags: list[str]) -> str:
    confidence = (confidence or "").lower()
    if "missing_max_benefit" in flags:
        return "review"
    if confidence == "high" and not CRITICAL_FLAGS.intersection(flags):
        return "auto"
    if "totals_mismatch" in flags and confidence in ("low", "medium"):
        return "retry_stronger_model"
    return "review"


# ---------------------------------------------------------------------------
# Flags calculados
# ---------------------------------------------------------------------------

def compute_flags(ext, lease, claim, doc_texts, charges_total, payments_total) -> list[str]:
    flags = []
    if ext.printed_total is not None and abs(charges_total - payments_total - ext.printed_total) > TOTALS_TOLERANCE:
        flags.append("totals_mismatch")
    if claim_amount_mismatch(claim.claim_amount, document_total(ext, charges_total)):
        flags.append("claim_amount_mismatch")
    if lease is not None and any(fee_exceeds(c, lease, claim.monthly_rent) for c in ext.charges):
        flags.append("fee_exceeds_lease_terms")
    if (lease is not None and lease.coverage_amount is not None and claim.max_benefit is not None
            and money(lease.coverage_amount) != money(claim.max_benefit)):
        flags.append("coverage_mismatch")
    if amounts_not_in_text(ext, doc_texts):
        flags.append("amount_not_in_text")
    if claim.monthly_rent is not None and any(
        c.category == "unpaid_rent" and not c.prorated and money(c.amount) != money(claim.monthly_rent)
        for c in ext.charges
    ):
        flags.append("rent_mismatch")
    return flags


def document_total(ext: Extraction, charges_total: Decimal) -> Decimal:
    """Total del documento: el impreso si existe; si no, la suma de los cargos."""
    return ext.printed_total if ext.printed_total is not None else charges_total


def claim_amount_mismatch(claim_amount: Decimal | None, doc_total: Decimal) -> bool:
    if claim_amount is None:
        return False
    if claim_amount < MIN_CLAIM_AMOUNT:
        return True
    if doc_total <= 0:
        return False
    return abs(claim_amount - doc_total) / doc_total > CLAIM_AMOUNT_TOLERANCE


def fee_limit(term: FeeTerm, monthly_rent: Decimal | None) -> Decimal | None:
    """Monto máximo que permite el término, o None si no se puede calcular.

    per_unit y per_day no se pueden: Prompt B no extrae cantidades (cuántas cerraduras, cuántos días).
    """
    if term.kind == "fixed":
        return term.value
    if term.kind == "percent_of_rent" and monthly_rent is not None:
        rate = term.value if term.value <= 1 else term.value / 100  # el contrato da "10" (10%); acepta 0.10
        return money(rate * monthly_rent)
    return None


def fee_exceeds(charge: Charge, lease: LeaseTerms, monthly_rent: Decimal | None) -> bool:
    """El cargo supera lo que el contrato define para su categoría.

    Si el contrato define varios términos para la categoría (p. ej. $50 + $10 por día), solo se marca
    cuando todos son calculables y el cargo supera el mayor; si alguno no lo es, no se puede afirmar.
    """
    terms = lease.fees.get(charge.category or "")
    if not terms:
        return False
    limits = [fee_limit(t, monthly_rent) for t in terms]
    if any(limit is None for limit in limits):
        return False
    return charge.amount > max(limits) + CENT / 2


def amount_variants(amount: Decimal) -> list[str]:
    """Cómo puede aparecer un monto en el texto: 1,234.56 / 1234.56 (y sin centavos si son .00)."""
    a = money(abs(amount))
    variants = [f"{a:,.2f}", f"{a:.2f}"]
    if a == a.to_integral_value():
        variants += [f"{a:,.0f}", f"{a:.0f}"]
    return variants


def amounts_not_in_text(ext: Extraction, doc_texts: dict[str, str]) -> list[str]:
    """Montos extraídos que no aparecen en el texto de su documento (solo documentos con texto)."""
    missing = []
    for item in [*ext.charges, *ext.payments]:
        text = doc_texts.get(item.source_document or "")
        if not text or len(text.strip()) < MIN_TEXT_CHARS:
            continue
        if not any(v in text for v in amount_variants(item.amount)):
            missing.append(f"{item.source_document}: {money(item.amount)}")
    return missing


# ---------------------------------------------------------------------------
# Justificación (plantillas, nunca el LLM)
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {  # categorías de Prompt B (schemas.Category)
    "unpaid_rent": "unpaid rent",
    "damages": "damages",
    "cleaning": "cleaning",
    "utilities": "utilities",
    "rekey": "rekey",
    "personal_property_removal": "property removal",
    "pet_fee": "pet fees",
    "late_fee": "late fees",
    "returned_payment_fee": "returned payment fees",
    "convenience_fee": "convenience fees",
    "legal_fee": "legal fees",
    "admin_fee": "admin fees",
    "early_termination_fee": "early termination",
    "holdover": "holdover",
    "liquidated_damages": "liquidated damages",
    "program_fee": "program fees",
    "normal_wear_and_tear": "normal wear and tear",
    "other": "other",
}

FLAG_MESSAGES = {
    "totals_mismatch": "Charges minus payments don't match the printed total ({printed_total}).",
    "claim_amount_mismatch": "The claimed amount ({claim_amount}) doesn't match the documented total ({document_total}).",
    "fee_exceeds_lease_terms": "A fee exceeds what the lease allows.",
    "coverage_mismatch": "The lease coverage doesn't match the max benefit ({max_benefit}).",
    "amount_not_in_text": "Some extracted amounts don't appear in their document's text.",
    "rent_mismatch": "Monthly rent charges don't match the claim's rent ({monthly_rent}).",
    "missing_max_benefit": "The claim has no max benefit: needs review.",
}


def usd(value: Decimal | None) -> str:
    if value is None:
        return "—"
    amount = money(value)
    return f"-${-amount:,.2f}" if amount < 0 else f"${amount:,.2f}"


def explain(ext, claim, results, charges_total, eligible_owed, excluded_total, recommended, cap_applied, flags) -> str:
    parts = [f"Documented charges: {usd(charges_total)}.", f"Eligible after payments: {usd(eligible_owed)}."]
    if ext.deposit_held:
        parts.append(f"Deducted the deposit held of {usd(ext.deposit_held)}.")
    if cap_applied:
        parts.append(f"Applied the cap of {usd(claim.max_benefit)}.")
    if excluded_total > 0:
        labels = list(dict.fromkeys(
            CATEGORY_LABELS.get(r.charge.category or "", r.charge.category or "other")
            for r in results if not r.eligible and r.balance > 0
        ))
        parts.append(f"Excluded: {usd(excluded_total)} ({', '.join(labels)}).")
    parts.append(f"Recommended: {usd(recommended)}.")

    values = {
        "printed_total": usd(ext.printed_total), "claim_amount": usd(claim.claim_amount),
        "document_total": usd(document_total(ext, charges_total)), "max_benefit": usd(claim.max_benefit),
        "monthly_rent": usd(claim.monthly_rent),
    }
    for flag in flags:
        if flag in FLAG_MESSAGES:
            parts.append("⚠ " + FLAG_MESSAGES[flag].format(**values))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Adaptadores: JSON guardado -> núcleo
# ---------------------------------------------------------------------------

def parse_money(value) -> Decimal | None:
    """1234.5, "1234.50", "$1,234.50", "(100.00)" -> Decimal. None o vacío -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return money(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    digits = re.sub(r"[^0-9.]", "", text)
    try:
        amount = money(digits)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _category(value) -> str | None:
    return str(value).strip().lower() if value else None


_PRORATED = re.compile(r"pro[\s-]?rat|partial|parcial", re.IGNORECASE)


def extraction_from_json(raw: dict) -> Extraction:
    """Traduce el JSON de Prompt B (schemas.ClaimExtraction) al núcleo.

    - deposit_held es un tipo dentro de payments_and_credits: se suma aparte y no entra al FIFO.
    - El esquema no marca la renta prorrateada: se reconoce por la descripción ("Prorated rent", "partial").
    """
    charges = [
        Charge(
            description=str(c.get("description") or ""),
            amount=parse_money(c.get("amount")) or ZERO,
            date=parse_date(c.get("date")),
            category=_category(c.get("category")),
            eligible=c.get("eligible"),
            reason=str(c.get("reason") or ""),
            source_document=c.get("source_document"),
            eligibility_basis=c.get("eligibility_basis"),
            lease_reference=c.get("lease_reference"),
            prorated=bool(_PRORATED.search(str(c.get("description") or ""))),
        )
        for c in raw.get("line_items") or []
    ]
    payments = [
        Payment(
            type=_category(p.get("type")) or "",
            amount=parse_money(p.get("amount")) or ZERO,
            date=parse_date(p.get("date")),
            description=str(p.get("description") or ""),
            source_document=p.get("source_document"),
        )
        for p in raw.get("payments_and_credits") or []
    ]
    return Extraction(
        charges=charges,
        payments=payments,
        deposit_held=money(sum((p.amount for p in payments if p.type == "deposit_held"), ZERO)),
        printed_total=parse_money(raw.get("printed_total")),
        confidence=str(raw.get("confidence") or ""),
        flags=[str(f) for f in raw.get("flags") or []],
    )


def lease_from_json(raw: dict | None) -> LeaseTerms | None:
    """Traduce el JSON de Prompt A (schemas.LeaseTerms) al núcleo."""
    if not raw:
        return None
    fees: dict[str, list[FeeTerm]] = {}
    for fee in raw.get("fees") or []:
        category = _category(fee.get("category"))
        kind = _category(fee.get("amount_type"))
        value = parse_money(fee.get("value"))
        if category and category != "other" and kind and value is not None:  # "other" no se puede emparejar
            fees.setdefault(category, []).append(FeeTerm(kind=kind, value=value))
    program = raw.get("program") or {}
    return LeaseTerms(coverage_amount=parse_money(program.get("coverage_amount")), fees=fees)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def build_recommendation(claim, extraction, lease):
    """Calcula la recomendación con RULES_VERSION. Devuelve un Recommendation sin guardar."""
    from claims.models import Recommendation

    lease_terms = lease_from_json(lease.raw_json) if lease is not None and lease.is_lease else None
    doc_texts = dict(claim.documents.values_list("file_name", "full_text"))
    result = evaluate(
        extraction_from_json(extraction.raw_json),
        lease_terms,
        ClaimData(max_benefit=claim.max_benefit, claim_amount=claim.claim_amount, monthly_rent=claim.monthly_rent),
        doc_texts,
    )
    return Recommendation(
        claim=claim,
        extraction=extraction,
        recommended_amount=result.recommended_amount,
        eligible_owed=result.eligible_owed,
        excluded_total=result.excluded_total,
        cap_applied=result.cap_applied,
        flags=result.flags,
        reasoning=[r.as_json() for r in result.charges],
        explanation=result.explanation,
        route=result.route,
        rules_version=RULES_VERSION,
    )
