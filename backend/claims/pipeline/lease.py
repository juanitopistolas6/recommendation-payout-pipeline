"""Análisis del contrato (Prompt A): convierte el lease en un JSON corto con lo que define el pago.

El LLM solo lee y extrae términos del contrato; no calcula montos (eso es rules.py).

Guardia: si el modelo dice que los documentos no son un contrato (document_check.is_lease = false),
se reclasifican con su detected_type y el claim sigue sin contrato (reglas por defecto).
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import transaction

from claims.llm.client import PromptFile, get_client, load_prompt, render_prompt
from claims.llm.errors import LLMStageNotConfigured
from claims.models import Claim, ClaimDocument, LeaseExtraction
from claims.pipeline.classify import PROMPT_A_TYPES

PROMPT_VERSION = "lease-v1"

DocType = ClaimDocument.DocType


def lease_documents(claim: Claim) -> list[ClaimDocument]:
    return list(claim.documents.filter(doc_type__in=PROMPT_A_TYPES).order_by("id"))


def lease_cache_key(docs: list[ClaimDocument], model: str) -> str:
    """Hash de (hashes de los documentos + PROMPT_VERSION + modelo). No depende del orden de los documentos."""
    material = "|".join(sorted(d.file_hash for d in docs)) + f"|{PROMPT_VERSION}|{model}"
    return hashlib.sha256(material.encode()).hexdigest()


def _default_stage():
    try:
        template = load_prompt("lease")
    except FileNotFoundError as exc:
        raise LLMStageNotConfigured("Falta claims/prompts/lease.md (Prompt A del apéndice)") from exc
    try:
        from claims.llm.schemas import LeaseTerms
    except ImportError as exc:
        raise LLMStageNotConfigured("Falta LeaseTerms en claims/llm/schemas.py (esquema del apéndice)") from exc
    return template, LeaseTerms


def analyze_lease(
    claim: Claim, client=None, template: str | None = None, schema=None, force: bool = False
) -> LeaseExtraction | None:
    """Devuelve la LeaseExtraction del claim, o None si no hay contrato (o resultó no serlo).

    `template` y `schema` son el Prompt A y LeaseTerms del apéndice; solo se pasan explícitos en tests.
    `force=True` ignora la caché y vuelve a llamar a Gemini.
    """
    docs = lease_documents(claim)
    if not docs:
        return None

    model = settings.GEMINI_MODEL_EXTRACT
    cached = None if force else LeaseExtraction.objects.filter(claim=claim, cache_key=lease_cache_key(docs, model)).first()
    if cached is not None:
        return cached if cached.is_lease else None

    if template is None or schema is None:
        template, schema = _default_stage()
    analyzer = _LeaseAnalyzer(claim, client or get_client(), model, template, schema)

    if any(d.classification_confidence == "medium" for d in docs):
        return analyzer.one_by_one(docs)
    return analyzer.all_together(docs)


class _LeaseAnalyzer:
    def __init__(self, claim, client, model, template, schema):
        self.claim = claim
        self.client = client
        self.model = model
        self.schema = schema
        move_out = claim.move_out_date.isoformat() if claim.move_out_date else "Not provided"
        self.prompt = render_prompt(template, move_out_date=move_out)

    def all_together(self, docs):
        result = self._call(docs)
        if not result.document_check.is_lease:
            self._reclassify(docs, result)
            self._save(docs, result, is_lease=False)
            return None
        return self._save(docs, result, is_lease=True)

    def one_by_one(self, docs):
        """Alguna clasificación tuvo confianza media: se aísla cada documento para que la guardia
        detecte exactamente cuál no es contrato."""
        confirmed = []
        last_result = None
        for doc in docs:
            last_result = self._call([doc])
            if last_result.document_check.is_lease:
                confirmed.append((doc, last_result))
            else:
                self._reclassify([doc], last_result)

        if not confirmed:
            self._save(docs, last_result, is_lease=False)
            return None
        if len(confirmed) == 1:
            doc, result = confirmed[0]
            return self._save([doc], result, is_lease=True)
        # Varios documentos sí son contrato (p. ej. lease + renovación): una extracción con todos juntos
        return self.all_together([doc for doc, _ in confirmed])

    def _call(self, docs):
        files = [PromptFile(*d.llm_source) for d in docs]
        return self.client.generate(
            model=self.model, purpose="lease", prompt=self.prompt, files=files, schema=self.schema, claim=self.claim,
        )

    @staticmethod
    def _reclassify(docs, result):
        detected = (result.document_check.detected_type or "").lower()
        if detected not in DocType.values or detected in (DocType.PENDING, *PROMPT_A_TYPES):
            detected = DocType.OTHER  # no es contrato: va a Prompt B
        for doc in docs:
            doc.doc_type = detected
            doc.classification_method = ClaimDocument.ClassificationMethod.PROMPT_A_GUARD
            doc.classification_confidence = ""
            doc.save(update_fields=["doc_type", "classification_method", "classification_confidence"])

    def _save(self, docs, result, is_lease):
        with transaction.atomic():
            extraction, _ = LeaseExtraction.objects.update_or_create(
                claim=self.claim,
                defaults={
                    "raw_json": result.model_dump(mode="json"),
                    "is_lease": is_lease,
                    "prompt_version": PROMPT_VERSION,
                    "model_used": self.model,
                    "cache_key": lease_cache_key(docs, self.model),
                },
            )
            extraction.documents.set(docs)
        return extraction
