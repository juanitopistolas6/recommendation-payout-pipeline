from django.contrib import admin

from .models import (
    Claim,
    ClaimDocument,
    ClaimExtraction,
    ClaimOutcome,
    LeaseExtraction,
    LLMCall,
    Recommendation,
)


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("tracking_id", "processing_status", "is_test_set", "claim_amount", "max_benefit", "claim_date")
    list_filter = ("processing_status", "is_test_set")
    search_fields = ("tracking_id", "lease_street_address", "property_management_company")


@admin.register(ClaimOutcome)
class ClaimOutcomeAdmin(admin.ModelAdmin):
    list_display = ("claim", "approved_benefit", "status", "approval_date")
    list_filter = ("status",)
    search_fields = ("claim__tracking_id",)


@admin.register(ClaimDocument)
class ClaimDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "file_name", "claim", "doc_type", "classification_method",
        "classification_confidence", "page_count", "has_scanned_pages",
    )
    list_filter = ("doc_type", "classification_method", "has_scanned_pages", "mime_type")
    search_fields = ("file_name", "claim__tracking_id", "file_hash")
    raw_id_fields = ("claim",)


@admin.register(LeaseExtraction)
class LeaseExtractionAdmin(admin.ModelAdmin):
    list_display = ("claim", "is_lease", "model_used", "prompt_version", "created_at")
    list_filter = ("is_lease", "model_used", "prompt_version")
    search_fields = ("claim__tracking_id", "cache_key")
    raw_id_fields = ("claim",)
    filter_horizontal = ("documents",)


@admin.register(ClaimExtraction)
class ClaimExtractionAdmin(admin.ModelAdmin):
    list_display = ("claim", "confidence", "model_used", "prompt_version", "created_at")
    list_filter = ("confidence", "model_used", "prompt_version")
    search_fields = ("claim__tracking_id", "cache_key")
    raw_id_fields = ("claim", "lease_extraction")


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = (
        "claim", "recommended_amount", "eligible_owed", "excluded_total",
        "cap_applied", "route", "rules_version", "created_at",
    )
    list_filter = ("route", "cap_applied", "rules_version")
    search_fields = ("claim__tracking_id",)
    raw_id_fields = ("claim", "extraction")


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "model", "purpose", "status", "claim", "input_tokens", "output_tokens", "latency_ms")
    list_filter = ("status", "purpose", "model")
    search_fields = ("claim__tracking_id", "error_detail")
    raw_id_fields = ("claim",)
    date_hierarchy = "created_at"
