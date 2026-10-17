"""Análisis del claim (Prompt B): extrae cada cargo, pago y crédito y juzga su elegibilidad
con los términos del contrato o, si no hay contrato, con las reglas por defecto.

El LLM lee, clasifica y juzga; no suma ni calcula el pago (eso es rules.py).

Los placeholders se rellenan SOLO desde Claim. ClaimOutcome (la decisión humana) nunca entra al prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging

from django.conf import settings

from claims.llm.client import PromptFile, get_client, load_prompt, render_prompt
from claims.llm.errors import LLMStageNotConfigured
from claims.models import Claim, ClaimDocument, ClaimExtraction, LeaseExtraction
from claims.pipeline.classify import LEASE_SIGNALS, documents_for_prompt_b

logger = logging.getLogger(__name__)

PROMPT_VERSION = "claim-v1"

NO_LEASE = "NO LEASE PROVIDED"
MISSING = "Not provided"  # valor del apéndice para placeholders vacíos
LEASE_INCLUDED_FLAG = "lease_document_included"

# Tipos de archivo que Gemini recibe como parte binaria
_SENDABLE_MIME_PREFIXES = ("application/pdf", "image/", "text/plain")


def claim_documents(claim: Claim) -> list[ClaimDocument]:
    """Todos los documentos que no son lease (incluye lease_mixed) y que se pueden mandar a Gemini."""
    docs = []
    for doc in documents_for_prompt_b(claim).order_by("id"):
        mime = doc.llm_source[1] or ""
        if mime.startswith(_SENDABLE_MIME_PREFIXES):
            docs.append(doc)
        else:
            logger.warning("Claim %s: %s (%s) no se manda a Prompt B", claim.pk, doc.file_name, mime or "sin mime")
    return docs


def _fmt(value) -> str:
    if value is None or value == "":
        return MISSING
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def lease_terms_json(lease: LeaseExtraction | None) -> str:
    if lease is None or not lease.is_lease:
        return NO_LEASE
    terms = {k: v for k, v in lease.raw_json.items() if k != "document_check"}
    return json.dumps(terms, indent=2, ensure_ascii=False, sort_keys=True)


def prompt_variables(claim: Claim, lease: LeaseExtraction | None, docs: list[ClaimDocument]) -> dict[str, str]:
    """Valores de los placeholders. Lee solo campos de Claim: nunca claim.outcome."""
    return {
        "tracking_number": claim.tracking_id,
        "monthly_rent": _fmt(claim.monthly_rent),
        "lease_start_date": _fmt(claim.lease_start_date),
        "lease_end_date": _fmt(claim.lease_end_date),
        "move_out_date": _fmt(claim.move_out_date),
        "termination_type": _fmt(claim.termination_type),
        "claim_amount": _fmt(claim.claim_amount),
        "pm_explanation": _fmt(claim.pm_explanation),
        "document_list": "\n".join(f"- {d.file_name} ({d.doc_type})" for d in docs) or "(no documents)",
        "lease_terms_json": lease_terms_json(lease),
    }


def claim_cache_key(docs, model: str, lease: LeaseExtraction | None, variables: dict[str, str]) -> str:
    """Hash de (hashes de documentos + PROMPT_VERSION + modelo + cache_key del contrato + datos del claim)."""
    material = json.dumps(
        {
            "documents": sorted(d.file_hash for d in docs),
            "prompt_version": PROMPT_VERSION,
            "model": model,
            "lease": lease.cache_key if lease is not None else None,
            "claim_data": variables,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _default_stage():
    try:
        template = load_prompt("claim")
    except FileNotFoundError as exc:
        raise LLMStageNotConfigured("Falta claims/prompts/claim.md (Prompt B del apéndice)") from exc
    try:
        # Mismo nombre que el modelo de Django ClaimExtraction: se importa con alias
        from claims.llm.schemas import ClaimExtraction as ClaimExtractionSchema
    except ImportError as exc:
        raise LLMStageNotConfigured("Falta ClaimExtraction en claims/llm/schemas.py") from exc
    return template, ClaimExtractionSchema


def analyze_claim(
    claim: Claim,
    lease: LeaseExtraction | None,
    model: str | None = None,
    client=None,
    template: str | None = None,
    schema=None,
    force: bool = False,
) -> ClaimExtraction:
    """`template` y `schema` son el Prompt B y su esquema del apéndice; solo se pasan explícitos en tests.
    `force=True` ignora la caché y vuelve a llamar a Gemini."""
    model = model or settings.GEMINI_MODEL_EXTRACT
    docs = claim_documents(claim)
    variables = prompt_variables(claim, lease, docs)
    key = claim_cache_key(docs, model, lease, variables)

    cached = None if force else ClaimExtraction.objects.filter(claim=claim, cache_key=key).first()
    if cached is not None:
        return cached

    if template is None or schema is None:
        template, schema = _default_stage()

    parsed = (client or get_client()).generate(
        model=model,
        purpose="retry" if model == settings.GEMINI_MODEL_RETRY else "claim",
        prompt=render_prompt(template, **variables),
        files=[PromptFile(*d.llm_source) for d in docs],
        schema=schema,
        claim=claim,
    )

    extraction = ClaimExtraction.objects.create(
        claim=claim,
        lease_extraction=lease if lease is not None and lease.is_lease else None,
        raw_json=parsed.model_dump(mode="json"),
        confidence=str(getattr(parsed, "confidence", "") or ""),
        prompt_version=PROMPT_VERSION,
        model_used=model,
        cache_key=key,
    )

    if lease is None or not lease.is_lease:
        _inverse_guard(claim, parsed, docs)
    return extraction


def _inverse_guard(claim: Claim, parsed, docs: list[ClaimDocument]) -> None:
    """El claim no tenía contrato pero Prompt B encontró uno entre los documentos: se reclasifica.

    No re-ejecuta nada; Prompt A lo tomará en la siguiente corrida manual.
    """
    if LEASE_INCLUDED_FLAG not in [str(f).lower() for f in (getattr(parsed, "flags", None) or [])]:
        return
    doc = identify_lease_document(docs, getattr(parsed, "findings", None) or [])
    if doc is None:
        logger.warning("Claim %s: Prompt B marcó %s pero no se pudo identificar el documento sin ambigüedad",
                       claim.pk, LEASE_INCLUDED_FLAG)
        return
    doc.doc_type = ClaimDocument.DocType.LEASE
    doc.classification_method = ClaimDocument.ClassificationMethod.PROMPT_B_GUARD
    doc.classification_confidence = ""
    doc.save(update_fields=["doc_type", "classification_method", "classification_confidence"])


def identify_lease_document(docs: list[ClaimDocument], findings: list[str]) -> ClaimDocument | None:
    """El esquema de Prompt B no dice qué documento es el contrato. Se busca, de lo más a lo menos seguro:

    1. Un finding que mencione "lease" y el nombre de exactamente un documento.
    2. El único documento con más señales de contrato (las de classify_by_rules), si llega al umbral de 2.
    Si ninguno es inequívoco, devuelve None.
    """
    for finding in findings:
        text = finding.lower()
        if "lease" not in text:
            continue
        named = [d for d in docs if d.file_name.lower() in text]
        if len(named) == 1:
            return named[0]

    scores = {d: lease_signal_score(d) for d in docs}
    best = max(scores.values(), default=0)
    leaders = [d for d, score in scores.items() if score == best]
    if best >= 2 and len(leaders) == 1:
        return leaders[0]
    return None


def lease_signal_score(doc: ClaimDocument) -> int:
    text = (doc.first_pages_text or "").lower()
    return sum(s in text for s in LEASE_SIGNALS) + 2 * ("lease" in doc.file_name.lower())
