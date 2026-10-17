import type { ClaimStatus, Money, Route } from "./api/types";

export type Tone = "neutral" | "progress" | "success" | "warning" | "danger" | "quota";

export const STATUS: Record<ClaimStatus, { label: string; tone: Tone }> = {
  pending: { label: "Pending", tone: "neutral" },
  queued: { label: "Queued", tone: "neutral" },
  classifying: { label: "Classifying documents", tone: "progress" },
  analyzing_lease: { label: "Analyzing lease", tone: "progress" },
  analyzing_claim: { label: "Analyzing claim", tone: "progress" },
  computing: { label: "Computing", tone: "progress" },
  done: { label: "Done", tone: "success" },
  needs_review: { label: "Needs review", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
  quota_exhausted: { label: "Quota exhausted", tone: "quota" },
  not_found: { label: "Not found", tone: "danger" },
};

export const ROUTES: Record<Route, string> = {
  auto: "Automatic payout",
  review: "Human review",
  retry_stronger_model: "Retry with stronger model",
};

// Flags from the rules engine (rules.FLAG_MESSAGES) and from the LLM (schemas.ClaimExtraction.flags)
export const FLAGS: Record<string, { label: string; message: string; critical?: boolean }> = {
  totals_mismatch: {
    label: "Totals don't match",
    message: "Charges minus payments don't add up to the total printed on the document.",
    critical: true,
  },
  claim_amount_mismatch: {
    label: "Claim amount differs",
    message: "The claimed amount doesn't match the documented total (or is under $10, a placeholder).",
  },
  fee_exceeds_lease_terms: {
    label: "Fee above lease terms",
    message: "A fee is higher than what the lease allows.",
  },
  coverage_mismatch: {
    label: "Coverage differs",
    message: "The coverage stated in the lease doesn't match the claim's max benefit.",
  },
  amount_not_in_text: {
    label: "Amount not found",
    message: "Some extracted amounts don't appear in their document's text: possible misread.",
  },
  rent_mismatch: {
    label: "Rent differs",
    message: "A full-month rent charge doesn't match the claim's monthly rent.",
  },
  missing_max_benefit: {
    label: "No max benefit",
    message: "The claim has no max benefit, so the cap can't be applied.",
    critical: true,
  },
  missing_documentation: {
    label: "Missing documentation",
    message: "The documents aren't enough to support the charges.",
    critical: true,
  },
  illegible_document: {
    label: "Illegible document",
    message: "A document can't be read reliably.",
    critical: true,
  },
  photos_only: {
    label: "Photos only",
    message: "There are only photos: no statement or invoices with amounts.",
    critical: true,
  },
  duplicate_charges: {
    label: "Duplicate charges",
    message: "Some charges seem to be repeated across documents.",
  },
  charges_after_move_out: {
    label: "Charges after move-out",
    message: "Some charges are dated after the tenant moved out.",
  },
  ambiguous_charges: {
    label: "Ambiguous charges",
    message: "Some charges don't have a clear description.",
  },
  lease_document_included: {
    label: "Lease included",
    message: "The lease was mixed in with the claim documents.",
  },
};

export function flagInfo(flag: string) {
  return FLAGS[flag] ?? { label: flag, message: "No description for this flag." };
}

// schemas.Category (Prompt B)
export const CATEGORIES: Record<string, string> = {
  unpaid_rent: "Unpaid rent",
  damages: "Damages",
  cleaning: "Cleaning",
  utilities: "Utilities",
  rekey: "Rekey",
  personal_property_removal: "Property removal",
  pet_fee: "Pet fee",
  late_fee: "Late fee",
  returned_payment_fee: "Returned payment",
  convenience_fee: "Convenience fee",
  legal_fee: "Legal fees",
  admin_fee: "Admin fee",
  early_termination_fee: "Early termination",
  holdover: "Holdover",
  liquidated_damages: "Liquidated damages",
  program_fee: "Program fee",
  normal_wear_and_tear: "Normal wear and tear",
  other: "Other",
};

export const DOC_TYPES: Record<string, string> = {
  pending: "Unclassified",
  lease: "Lease",
  lease_mixed: "Lease + other",
  ledger: "Ledger",
  move_out_statement: "Move-out statement",
  invoice: "Invoice",
  estimate: "Estimate",
  inspection_report: "Inspection report",
  photo: "Photo",
  correspondence: "Correspondence",
  other: "Other",
  unknown: "Unknown",
};

export const PAYMENT_TYPES: Record<string, string> = {
  payment: "Payment",
  credit: "Credit",
  concession: "Concession",
  deposit_held: "Deposit held",
};

export const ELIGIBILITY_SOURCES: Record<string, string> = {
  override: "fixed rule",
  llm: "LLM",
  default: "default",
};

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function money(value: Money | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? usd.format(n) : String(value);
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return "—";
  // A bare "YYYY-MM-DD" is shown as is, so the time zone can't shift it by a day
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : new Date(value).toLocaleString("en-US");
}

/** From quota_reason() in the backend: "... (2026-10-18 07:00 UTC)." -> the reset time, local. */
export function quotaResetTime(reason: string | null | undefined): string | null {
  const match = reason?.match(/(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC/);
  if (!match) return null;
  const reset = new Date(`${match[1]}T${match[2]}:00Z`);
  return Number.isNaN(reset.getTime())
    ? null
    : reset.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

/** Splits on commas, spaces or new lines; drops blanks and duplicates, keeping the order. */
export function parseIds(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).filter(Boolean))];
}
