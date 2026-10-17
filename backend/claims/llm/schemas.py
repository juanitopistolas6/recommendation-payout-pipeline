from typing import Literal
from pydantic import BaseModel

Confidence = Literal["high", "medium", "low"]
DocType = Literal["lease", "ledger", "move_out_statement", "invoice", "estimate",
                  "inspection_report", "photo", "correspondence", "other"]

# ---------- Prompt C ----------

class ClassifiedDocument(BaseModel):
    id: str
    type: DocType
    mixed: bool
    confidence: Confidence
    reason: str

class ClassificationBatch(BaseModel):
    documents: list[ClassifiedDocument]

# ---------- Prompt A ----------

class DocumentCheck(BaseModel):
    is_lease: bool
    detected_type: Literal["lease", "ledger", "invoice", "move_out_statement",
                           "inspection_report", "photo", "correspondence", "other"]
    contains_other_documents: bool
    reason: str

class ProgramTerms(BaseModel):
    option_selected: Literal["deposit_waiver_program", "traditional_deposit", "unclear"]
    coverage_amount: float | None
    traditional_deposit_amount: float | None
    monthly_program_fee: float | None
    coverage_language: str | None
    section_reference: str | None

class AllowedDeduction(BaseModel):
    category: Literal["damages", "unpaid_rent", "utilities", "pet_fee", "cleaning",
                      "personal_property_removal", "late_fee", "other_fees", "rekey",
                      "lease_violation_costs", "other"]
    quote: str
    section_reference: str | None

class LeaseFee(BaseModel):
    name: str
    category: Literal["late_fee", "returned_payment_fee", "rekey", "early_termination_fee",
                      "holdover", "liquidated_damages", "admin_fee", "pet_fee",
                      "convenience_fee", "other"]
    amount_type: Literal["fixed", "percent_of_rent", "per_unit", "per_day"]
    value: float
    unit: str | None
    section_reference: str | None

class PetTerms(BaseModel):
    allowed: bool | None
    notes: str | None

class UncertainSelection(BaseModel):
    clause: str
    section_reference: str | None

class Inconsistency(BaseModel):
    description: str
    values: list[str]

class LeaseTerms(BaseModel):
    document_check: DocumentCheck                  # siempre primero
    program: ProgramTerms | None
    monthly_rent: float | None
    allowed_deductions: list[AllowedDeduction]
    fees: list[LeaseFee]
    normal_wear_and_tear_excluded: bool | None
    attorney_fees_recoverable: bool | None
    pets: PetTerms | None
    uncertain_selections: list[UncertainSelection]
    inconsistencies: list[Inconsistency]
    confidence: Confidence

# ---------- Prompt B ----------

Category = Literal["unpaid_rent", "damages", "cleaning", "utilities", "rekey",
                   "personal_property_removal", "pet_fee", "late_fee",
                   "returned_payment_fee", "convenience_fee", "legal_fee", "admin_fee",
                   "early_termination_fee", "holdover", "liquidated_damages",
                   "program_fee", "normal_wear_and_tear", "other"]

class LineItem(BaseModel):
    date: str | None
    description: str
    amount: float
    category: Category
    eligible: bool
    eligibility_basis: Literal["lease", "program_default"]
    lease_reference: str | None
    reason: str
    source_document: str

class PaymentCredit(BaseModel):
    date: str | None
    description: str
    amount: float
    type: Literal["payment", "credit", "concession", "deposit_held"]
    source_document: str

class ClaimExtraction(BaseModel):
    line_items: list[LineItem]
    payments_and_credits: list[PaymentCredit]
    printed_total: float | None
    findings: list[str]
    flags: list[Literal["missing_documentation", "illegible_document", "photos_only",
                        "duplicate_charges", "charges_after_move_out",
                        "ambiguous_charges", "lease_document_included"]]
    confidence: Confidence
