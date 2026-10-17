// Mirror the schemas in backend/claims/api.py and the protocol in backend/claims/sse.py.
// Decimals arrive as strings; amounts that come raw from the LLM (payments) arrive as numbers.

export type ClaimStatus =
  | "pending"
  | "queued"
  | "classifying"
  | "analyzing_lease"
  | "analyzing_claim"
  | "computing"
  | "done"
  | "needs_review"
  | "failed"
  | "quota_exhausted"
  | "not_found";

export const FINAL_STATUSES: ReadonlySet<ClaimStatus> = new Set<ClaimStatus>([
  "done",
  "needs_review",
  "failed",
  "quota_exhausted",
  "not_found",
]);

export type Route = "auto" | "review" | "retry_stronger_model";

export type Money = string | number;

export type Summary = {
  recommended_amount: string | null;
  route: Route;
  flags: string[];
  cap_applied: boolean;
};

// POST /api/claims/analyze
export type AnalyzeItem = {
  tracking_number: string;
  status: ClaimStatus;
  queue_position: number | null;
  summary: Summary | null;
  failure_reason: string | null;
};

// GET /api/claims/stream -> event: claim
export type StreamClaimState = {
  tracking_number: string;
  status: ClaimStatus;
  updated_at: string | null;
  failure_reason?: string | null;
  summary?: Summary | null;
  queue_position?: number | null;
};

// GET /api/claims/{tracking_number}
export type ClaimData = {
  tracking_number: string;
  claim_date: string | null;
  lease_street_address: string;
  lease_city: string;
  lease_state: string;
  lease_zip: string;
  lease_start_date: string | null;
  lease_end_date: string | null;
  move_out_date: string | null;
  monthly_rent: string | null;
  termination_type: string;
  claim_amount: string | null;
  max_benefit: string | null;
  policy: string;
  property_management_company: string;
  property_manager_name: string;
  pm_explanation: string;
};

export type ClaimDocument = {
  id: number;
  file_name: string;
  doc_type: string;
  classification_method: string;
  classification_confidence: string;
  page_count: number | null;
  has_scanned_pages: boolean;
};

// Recommendation.reasoning (rules.ChargeResult.as_json)
export type Charge = {
  description: string;
  date: string | null;
  category: string | null;
  amount: string;
  eligible: boolean;
  eligibility_source?: "override" | "llm" | "default";
  eligibility_basis?: string | null;
  lease_reference?: string | null;
  reason: string;
  paid?: string;
  balance?: string;
  source_document?: string | null;
};

// Prompt B: payments_and_credits
export type Payment = {
  date: string | null;
  description: string;
  amount: Money;
  type: "payment" | "credit" | "concession" | "deposit_held";
  source_document?: string;
};

export type Recommendation = {
  recommended_amount: string | null;
  eligible_owed: string | null;
  excluded_total: string | null;
  cap_applied: boolean;
  explanation: string;
  flags: string[];
  route: Route;
  rules_version: string;
  created_at: string;
  charges: Charge[];
  payments: Payment[];
  findings: string[] | Record<string, unknown> | null;
  model_used: string;
  extraction_confidence: string;
};

export type ClaimDetail = {
  claim: ClaimData;
  status: ClaimStatus;
  failure_reason: string;
  queue_position: number | null;
  documents: ClaimDocument[];
  recommendation: Recommendation | null;
};

// GET /api/metrics. Amounts are JSON numbers here (the rest of the API sends money as strings).
// With n = 0 every metric is null.
export type MetricsSummary = {
  rules_version: string;
  n: number;
  ai_mae: number | null;
  ai_bias: number | null; // mean(AI - human): negative = the AI pays less than the human
  baseline_name: string;
  baseline_description: string;
  baseline_mae: number | null;
  improvement_pct: number | null; // negative = the AI has more error than the simple rule
};

// GET /api/metrics/claims, largest abs_error first
export type MetricsClaimRow = {
  tracking_number: string;
  human: number;
  ai: number;
  difference: number; // AI - human
  abs_error: number;
  route: Route;
  flags: string[];
};
