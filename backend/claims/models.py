import mimetypes

from django.db import models

MONEY = {"max_digits": 12, "decimal_places": 2, "null": True, "blank": True}


class Claim(models.Model):
    """Datos del Excel que el pipeline SÍ puede usar como entrada.

    La decisión humana (monto aprobado, status, cobranza...) vive en ClaimOutcome,
    que el pipeline nunca importa. Solo lo lee el módulo de métricas.
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending"
        QUEUED = "queued"
        CLASSIFYING = "classifying"
        ANALYZING_LEASE = "analyzing_lease"
        ANALYZING_CLAIM = "analyzing_claim"
        COMPUTING = "computing"
        DONE = "done"
        NEEDS_REVIEW = "needs_review"
        FAILED = "failed"
        QUOTA_EXHAUSTED = "quota_exhausted"

    # Identificación: el Tracking Number del Excel es la llave primaria (no hay id autoincremental)
    tracking_id = models.CharField(max_length=32, primary_key=True)
    claim_date = models.DateField(null=True, blank=True)

    # Propiedad
    lease_street_address = models.CharField(max_length=255, blank=True)
    lease_city = models.CharField(max_length=128, blank=True)
    lease_state = models.CharField(max_length=32, blank=True)
    lease_zip = models.CharField(max_length=16, blank=True)  # texto: conserva "29485-6804" y ceros a la izquierda

    # Contrato
    lease_start_date = models.DateField(null=True, blank=True)
    lease_end_date = models.DateField(null=True, blank=True)
    move_out_date = models.DateField(null=True, blank=True)
    monthly_rent = models.DecimalField(**MONEY)
    termination_type = models.CharField(max_length=64, blank=True)

    # Claim y póliza
    claim_amount = models.DecimalField(**MONEY)  # puede ser un placeholder (ej. $1.00)
    max_benefit = models.DecimalField(**MONEY)
    policy = models.CharField(max_length=64, blank=True)
    group_number = models.CharField(max_length=64, blank=True)
    treaty_number = models.CharField(max_length=64, blank=True)

    # Property manager
    property_management_company = models.CharField(max_length=255, blank=True)
    property_manager_name = models.CharField(max_length=255, blank=True)
    pm_explanation = models.TextField(blank=True)

    # Fila original completa SIN las columnas de resultado (ver ClaimOutcome)
    raw_row = models.JSONField(default=dict)
    source_file = models.CharField(max_length=255, blank=True)
    source_row = models.IntegerField(null=True, blank=True)  # número de fila en el Excel

    # Evaluación y procesamiento
    is_test_set = models.BooleanField(default=False, db_index=True)
    processing_status = models.CharField(
        max_length=32, choices=ProcessingStatus, default=ProcessingStatus.PENDING, db_index=True
    )
    failure_reason = models.TextField(blank=True)

    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "claim"
        ordering = ["source_row"]  # mismo orden que el Excel

    def __str__(self):
        return f"Claim {self.tracking_id}"


class ClaimOutcome(models.Model):
    """Resultado de la revisión humana. NUNCA debe entrar a un prompt.

    Incluye todo lo que se llena después de la decisión (aprobación, cobranza,
    acuerdos con el inquilino) porque revelaría la respuesta.
    """

    # La llave primaria es el propio claim, en la columna tracking_id (no hay id autoincremental)
    claim = models.OneToOneField(
        Claim, on_delete=models.CASCADE, primary_key=True, db_column="tracking_id", related_name="outcome"
    )

    # Decisión
    approved_benefit = models.DecimalField(**MONEY)  # ground truth para el MAE
    status = models.CharField(max_length=64, blank=True)
    approval_date = models.DateField(null=True, blank=True)
    posted_date = models.DateField(null=True, blank=True)
    pending_docs_from_pm = models.CharField(max_length=255, blank=True)
    hold_reason = models.TextField(blank=True)

    # Cobranza al inquilino
    tenant_contacted = models.CharField(max_length=64, blank=True)
    tenant_collection_status = models.CharField(max_length=128, blank=True)
    collection_status = models.CharField(max_length=128, blank=True)
    send_to_collections = models.BooleanField(null=True, blank=True)
    agreed_tenant_settlement = models.DecimalField(**MONEY)
    agreed_tenant_settlement_date = models.DateField(null=True, blank=True)
    collected_date = models.DateField(null=True, blank=True)
    collected_amount = models.DecimalField(**MONEY)
    collection_processed_date = models.DateField(null=True, blank=True)

    audit_selection = models.BooleanField(null=True, blank=True)

    # Columnas de resultado tal cual venían en el Excel
    raw_outcome = models.JSONField(default=dict)

    class Meta:
        db_table = "claims_outcome"

    def __str__(self):
        return f"Outcome {self.claim_id}: {self.approved_benefit}"


# ---------------------------------------------------------------------------
# Pipeline: cada paso guarda su resultado para no repetir trabajo costoso
# ---------------------------------------------------------------------------


class ClaimDocument(models.Model):
    """Un archivo de soporte del claim (PDF, docx, imagen) con su texto y su clasificación."""

    class DocType(models.TextChoices):
        PENDING = "pending"
        LEASE = "lease"
        LEASE_MIXED = "lease_mixed"
        LEDGER = "ledger"
        MOVE_OUT_STATEMENT = "move_out_statement"
        INVOICE = "invoice"
        ESTIMATE = "estimate"
        INSPECTION_REPORT = "inspection_report"
        PHOTO = "photo"
        CORRESPONDENCE = "correspondence"
        OTHER = "other"
        UNKNOWN = "unknown"

    class ClassificationMethod(models.TextChoices):
        EXTENSION = "extension"
        RULES = "rules"
        LLM = "llm"
        PROMPT_A_GUARD = "prompt_a_guard"
        PROMPT_B_GUARD = "prompt_b_guard"
        MANUAL = "manual"

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="documents")

    file_path = models.CharField(max_length=500)  # relativa a DATA_ROOT: claims/<tracking_id>/... (claims/paths.py)
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 en hex
    mime_type = models.CharField(max_length=128, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    has_scanned_pages = models.BooleanField(default=False)
    first_pages_text = models.TextField(blank=True)
    full_text = models.TextField(blank=True)
    converted_pdf_path = models.CharField(max_length=500, blank=True)  # conversión (PDF de un docx, JPG de un HEIC): _converted/...

    doc_type = models.CharField(max_length=32, choices=DocType, default=DocType.PENDING, db_index=True)
    classification_method = models.CharField(max_length=32, choices=ClassificationMethod, blank=True)
    classification_confidence = models.CharField(max_length=16, blank=True)

    class Meta:
        db_table = "claim_document"
        unique_together = ("claim", "file_hash")
        ordering = ["claim", "file_name"]

    def __str__(self):
        return f"{self.claim_id} / {self.file_name} ({self.doc_type})"

    @property
    def llm_source(self) -> tuple[str, str]:
        """(ruta absoluta, mime_type) del archivo que se manda a Gemini: el convertido (PDF de un docx, JPG de
        un HEIC) si existe; si no, el original."""
        from claims.paths import resolve_data_path

        if self.converted_pdf_path:
            mime = mimetypes.guess_type(self.converted_pdf_path)[0] or "application/pdf"
            return str(resolve_data_path(self.converted_pdf_path)), mime
        return str(resolve_data_path(self.file_path)), self.mime_type


class LeaseExtraction(models.Model):
    """Lo que el LLM leyó del contrato de arrendamiento. Uno por claim."""

    claim = models.OneToOneField(Claim, on_delete=models.CASCADE, related_name="lease_extraction")
    documents = models.ManyToManyField(ClaimDocument, related_name="lease_extractions", blank=True)
    raw_json = models.JSONField(default=dict)
    is_lease = models.BooleanField(default=False)
    prompt_version = models.CharField(max_length=32)
    model_used = models.CharField(max_length=64)
    cache_key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Lease {self.claim_id} ({self.model_used})"

    class Meta:
        db_table = "lease_extraction"
        ordering = ["-created_at"]


class ClaimExtraction(models.Model):
    """Lo que el LLM leyó y clasificó de los cargos del claim. Nunca contiene montos calculados."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="claim_extractions")
    lease_extraction = models.ForeignKey(
        LeaseExtraction, on_delete=models.SET_NULL, null=True, blank=True, related_name="claim_extractions"
    )
    raw_json = models.JSONField(default=dict)
    confidence = models.CharField(max_length=16, blank=True)
    prompt_version = models.CharField(max_length=32)
    model_used = models.CharField(max_length=64)
    cache_key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "claim_extraction"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Extraction {self.claim_id} ({self.model_used})"


class Recommendation(models.Model):
    """Pago recomendado calculado por el código (rules.py), nunca por el LLM. Una por versión de reglas."""

    class Route(models.TextChoices):
        AUTO = "auto"
        REVIEW = "review"
        RETRY_STRONGER_MODEL = "retry_stronger_model"

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="recommendations")
    extraction = models.ForeignKey(ClaimExtraction, on_delete=models.CASCADE, related_name="recommendations")

    recommended_amount = models.DecimalField(**MONEY)
    eligible_owed = models.DecimalField(**MONEY)
    excluded_total = models.DecimalField(**MONEY)
    cap_applied = models.BooleanField(default=False)

    flags = models.JSONField(default=list)
    reasoning = models.JSONField(default=list)  # cada cargo con su elegibilidad, razón y saldo
    explanation = models.TextField(blank=True)

    route = models.CharField(max_length=32, choices=Route, db_index=True)
    rules_version = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recommendation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["claim", "rules_version"], name="unique_recommendation_per_rules_version"),
        ]

    def __str__(self):
        return f"Recommendation {self.claim_id} v{self.rules_version}: {self.recommended_amount}"


class LLMCall(models.Model):
    """Registro de cada llamada al LLM: tokens, latencia y errores (cuota, salida inválida...)."""

    class Purpose(models.TextChoices):
        CLASSIFY = "classify"
        LEASE = "lease"
        CLAIM = "claim"
        RETRY = "retry"

    class Status(models.TextChoices):
        OK = "ok"
        RATE_LIMITED_MINUTE = "rate_limited_minute"
        RATE_LIMITED_DAY = "rate_limited_day"
        INVALID_OUTPUT = "invalid_output"
        ERROR = "error"

    model = models.CharField(max_length=64)
    purpose = models.CharField(max_length=16, choices=Purpose)
    claim = models.ForeignKey(Claim, on_delete=models.SET_NULL, null=True, blank=True, related_name="llm_calls")
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status, db_index=True)
    error_detail = models.TextField(blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.model} {self.purpose} {self.status}"