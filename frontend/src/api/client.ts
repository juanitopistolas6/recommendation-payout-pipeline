import type { AnalyzeItem, ClaimDetail, MetricsClaimRow, MetricsSummary } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// ninja returns {"detail": "..."} or, on validation errors, {"detail": [{"msg": "..."}]}
async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ");
    }
  } catch {
    // empty or non-JSON body
  }
  return `${res.status} ${res.statusText}`;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res));
  return (await res.json()) as T;
}

export function analyzeClaims(trackingNumbers: string[], force: boolean, signal?: AbortSignal) {
  return request<AnalyzeItem[]>("/api/claims/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tracking_numbers: trackingNumbers, force }),
    signal,
  });
}

export function getClaim(trackingNumber: string, signal?: AbortSignal) {
  return request<ClaimDetail>(`/api/claims/${encodeURIComponent(trackingNumber)}`, { signal });
}

export function streamUrl(ids: string[]) {
  return `/api/claims/stream?ids=${ids.map(encodeURIComponent).join(",")}`;
}

// No rules version = the backend's current one
function metricsQuery(rulesVersion?: string) {
  return rulesVersion ? `?rules_version=${encodeURIComponent(rulesVersion)}` : "";
}

export function getMetrics(rulesVersion: string | undefined, signal?: AbortSignal) {
  return request<MetricsSummary>(`/api/metrics${metricsQuery(rulesVersion)}`, { signal });
}

export function getMetricsClaims(rulesVersion: string | undefined, signal?: AbortSignal) {
  return request<MetricsClaimRow[]>(`/api/metrics/claims${metricsQuery(rulesVersion)}`, { signal });
}
