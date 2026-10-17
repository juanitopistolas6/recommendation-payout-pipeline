"""Clasificación de documentos: del método más barato al más caro.

1. Por extensión: jpg, jpeg, png, heic -> photo.
2. Reglas locales sobre el nombre y el texto de las primeras páginas.
3. LLM en lote (Prompt C, gemini-3.1-flash-lite), solo para lo que sigue unknown.

Ruteo posterior:
    lease        -> Prompt A
    lease_mixed  -> Prompt A y Prompt B
    cualquier otro (incluido unknown) -> Prompt B
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from claims.llm.errors import LLMStageNotConfigured
from claims.models import Claim, ClaimDocument

logger = logging.getLogger(__name__)

PROMPT_VERSION = "classify-v1"

DocType = ClaimDocument.DocType
Method = ClaimDocument.ClassificationMethod

LLM_BATCH_SIZE = 20
MAX_IMAGE_SIDE = 1600


# ---------------------------------------------------------------------------
# 1. Extensión
# ---------------------------------------------------------------------------

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}


def classify_by_extension(doc: ClaimDocument) -> str | None:
    return DocType.PHOTO if Path(doc.file_name).suffix.lower() in PHOTO_EXTENSIONS else None


# ---------------------------------------------------------------------------
# 2. Reglas locales
# ---------------------------------------------------------------------------

LEASE_SIGNALS = ["lease agreement", "residential lease", "lease for residential",
                 "landlord and tenant", "term of lease", "security deposit addendum"]
CHARGE_SIGNALS = ["balance due", "move out", "move-out", "invoice", "amount due",
                  "statement", "ledger", "total due"]


def classify_by_rules(doc) -> tuple[str, str]:
    name, text = doc.file_name.lower(), doc.first_pages_text.lower()
    if len(text.strip()) < 100:
        return "unknown", "no_text"
    lease_score = sum(s in text for s in LEASE_SIGNALS) + 2 * ("lease" in name)
    charge_score = sum(s in text for s in CHARGE_SIGNALS)
    if lease_score >= 2 and (doc.page_count or 0) >= 5:
        if charge_score >= 2 and "balance due" in doc.full_text[-3000:].lower():
            return "lease_mixed", "rules"
        return "lease", "rules"
    if charge_score >= 2 and lease_score == 0:
        return "charges", "rules"
    return "unknown", "ambiguous"


# ---------------------------------------------------------------------------
# 3. LLM en lote
# ---------------------------------------------------------------------------

@dataclass
class LLMDocResult:
    """Lo que el Prompt C dice de un documento, ya normalizado."""

    document_id: str
    doc_type: str
    confidence: str
    mixed: bool = False


def decide_from_llm(result: LLMDocResult) -> str:
    """Convierte la respuesta del LLM en doc_type según las reglas de confianza."""
    confidence = result.confidence.lower()
    doc_type = result.doc_type.lower()

    if confidence == "low":
        return DocType.OTHER  # confianza baja: se trata como documento de cargos
    if doc_type in (DocType.LEASE, DocType.LEASE_MIXED):
        return DocType.LEASE_MIXED if (result.mixed or doc_type == DocType.LEASE_MIXED) else DocType.LEASE
    if doc_type in DocType.values and doc_type not in (DocType.PENDING, DocType.UNKNOWN):
        return doc_type
    # 'unknown' o un tipo fuera de la lista: va a Prompt B igual, y no se vuelve a mandar al LLM
    return DocType.OTHER


def apply_llm_results(docs: list[ClaimDocument], results: list[LLMDocResult]) -> list[ClaimDocument]:
    """Asigna lo que devolvió el LLM. Los IDs que no volvieron quedan unknown. Devuelve los faltantes."""
    by_id = {str(r.document_id).strip(): r for r in results}
    missing = []
    for doc in docs:
        result = by_id.get(str(doc.id))
        if result is None:
            missing.append(doc)
            continue
        _save(doc, decide_from_llm(result), Method.LLM, result.confidence.lower())
    return missing


def first_page_file(doc: ClaimDocument, out_dir: Path):
    """Primera página lista para mandar: PDF de 1 página (pypdf) o imagen de máx. 1600 px (Pillow).

    Devuelve un PromptFile con la línea "Document ID: <id>" antes, o None si no se puede generar.
    """
    from claims.llm.client import PromptFile
    from claims.paths import resolve_data_path

    source = resolve_data_path(doc.converted_pdf_path or doc.file_path)
    suffix = source.suffix.lower()
    label = f"Document ID: {doc.id}"
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader, PdfWriter

            reader = PdfReader(source)
            writer = PdfWriter()
            writer.add_page(reader.pages[0])
            target = out_dir / f"{doc.id}.pdf"
            with target.open("wb") as fh:
                writer.write(fh)
            return PromptFile(str(target), "application/pdf", label=label)

        if (doc.mime_type or "").startswith("image/") or suffix in {".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp"}:
            from PIL import Image, ImageOps

            with Image.open(source) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
                target = out_dir / f"{doc.id}.jpg"
                img.save(target, "JPEG", quality=85)
            return PromptFile(str(target), "image/jpeg", label=label)
    except Exception as exc:  # noqa: BLE001 — un archivo roto no frena el lote
        logger.warning("No se pudo extraer la primera página de %s: %s", source, exc)
    return None


def _llm_stage():
    """Prompt C y su esquema. Se cargan aquí para que el resto del módulo funcione sin ellos."""
    from claims.llm.client import load_prompt, render_prompt

    try:
        template = load_prompt("classify")
    except FileNotFoundError as exc:
        raise LLMStageNotConfigured("Falta claims/prompts/classify.md (Prompt C del apéndice)") from exc
    try:
        from claims.llm.schemas import ClassificationBatch
    except ImportError as exc:
        raise LLMStageNotConfigured("Falta ClassificationBatch en claims/llm/schemas.py") from exc
    return render_prompt(template), ClassificationBatch


def _to_results(parsed) -> list[LLMDocResult]:
    """Adapta ClassificationBatch (Prompt C) al formato interno."""
    return [
        LLMDocResult(
            document_id=str(item.id),
            doc_type=item.type,
            confidence=item.confidence,
            mixed=item.mixed,
        )
        for item in parsed.documents
    ]


def classify_with_llm(claim: Claim, docs: list[ClaimDocument], client=None) -> None:
    from claims.llm.client import get_client

    prompt, schema = _llm_stage()
    client = client or get_client()

    for start in range(0, len(docs), LLM_BATCH_SIZE):
        batch = docs[start:start + LLM_BATCH_SIZE]
        with tempfile.TemporaryDirectory() as tmp:
            sendable, files = [], []
            for doc in batch:
                page = first_page_file(doc, Path(tmp))
                if page is not None:  # sin primera página utilizable, queda unknown
                    sendable.append(doc)
                    files.append(page)
            if not files:
                continue
            parsed = client.generate(
                model=settings.GEMINI_MODEL_CLASSIFY, purpose="classify",
                prompt=prompt, files=files, schema=schema, claim=claim,
            )
        missing = apply_llm_results(sendable, _to_results(parsed))
        if missing:
            logger.warning("Claim %s: el LLM no devolvió %d documento(s): %s",
                           claim.pk, len(missing), [d.id for d in missing])


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------

def _save(doc: ClaimDocument, doc_type: str, method: str, confidence: str = "") -> None:
    doc.doc_type = doc_type
    doc.classification_method = method
    doc.classification_confidence = confidence
    doc.save(update_fields=["doc_type", "classification_method", "classification_confidence"])


def classify_claim_documents(claim: Claim, use_llm: bool = True, client=None) -> None:
    """Clasifica los documentos del claim en pending o unknown. Cada resultado se guarda al momento."""
    pending_llm = []
    for doc in claim.documents.filter(doc_type__in=[DocType.PENDING, DocType.UNKNOWN]).order_by("id"):
        if classify_by_extension(doc):
            _save(doc, DocType.PHOTO, Method.EXTENSION)
            continue

        doc_type, _reason = classify_by_rules(doc)
        if doc_type == "charges":
            _save(doc, DocType.OTHER, Method.RULES)  # tipo genérico de cargos: va a Prompt B
        elif doc_type in (DocType.LEASE, DocType.LEASE_MIXED):
            _save(doc, doc_type, Method.RULES)
        else:
            _save(doc, DocType.UNKNOWN, "")
            pending_llm.append(doc)

    if use_llm and pending_llm:
        classify_with_llm(claim, pending_llm, client=client)


# ---------------------------------------------------------------------------
# Ruteo
# ---------------------------------------------------------------------------

PROMPT_A_TYPES = (DocType.LEASE, DocType.LEASE_MIXED)


def prompts_for(doc_type: str) -> set[str]:
    if doc_type == DocType.LEASE:
        return {"A"}
    if doc_type == DocType.LEASE_MIXED:
        return {"A", "B"}
    return {"B"}


def documents_for_prompt_a(claim: Claim):
    return claim.documents.filter(doc_type__in=PROMPT_A_TYPES)


def documents_for_prompt_b(claim: Claim):
    return claim.documents.exclude(doc_type=DocType.LEASE)
