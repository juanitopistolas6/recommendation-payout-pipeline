import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { getMetrics, getMetricsClaims } from "../api/client";
import type { MetricsClaimRow, MetricsSummary } from "../api/types";
import { ROUTES, flagInfo, money } from "../labels";
import { ClaimDetailPanel } from "./ClaimDetailPanel";
import type { Row } from "./ResultsTable";

type Load =
  | { kind: "loading" }
  | { kind: "ok"; summary: MetricsSummary; claims: MetricsClaimRow[] }
  | { kind: "error"; message: string };

const wholeUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

/** "$663" for the cards: the cents don't help to read them at a glance. */
function whole(value: number | null): string {
  return value === null ? "—" : wholeUsd.format(value);
}

/** Explicit sign: "−$523" / "+$40" / "$0". */
function signed(value: number, format: (n: number) => string): string {
  if (value === 0) return format(0);
  return `${value < 0 ? "−" : "+"}${format(Math.abs(value))}`;
}

export function MetricsView() {
  const [rulesVersion, setRulesVersion] = useState("");
  // A new object on every Refresh or Retry, so it refetches even if the version didn't change
  const [query, setQuery] = useState<{ rulesVersion?: string }>({});
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const { rulesVersion: version } = query;
    Promise.all([getMetrics(version, controller.signal), getMetricsClaims(version, controller.signal)])
      .then(([summary, claims]) => setLoad({ kind: "ok", summary, claims }))
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setLoad({ kind: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => controller.abort();
  }, [query]);

  const refetch = (version: string | undefined) => {
    setLoad({ kind: "loading" });
    setSelected(null);
    setQuery({ rulesVersion: version });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    refetch(rulesVersion.trim() || undefined);
  };

  const claims = load.kind === "ok" ? load.claims : [];
  const selectedClaim = claims.find((c) => c.tracking_number === selected) ?? null;

  return (
    <div className="metrics">
      <form className="metrics-controls" onSubmit={submit}>
        <label>
          Rules version
          <input
            type="text"
            value={rulesVersion}
            onChange={(e) => setRulesVersion(e.target.value)}
            placeholder="current"
            spellCheck={false}
          />
        </label>
        <button type="submit">Refresh</button>
      </form>

      {load.kind === "loading" && <MetricsSkeleton />}

      {load.kind === "error" && (
        <div className="notice notice-danger" role="alert">
          <strong>Couldn't load the metrics</strong>
          <div>{load.message}</div>
          <button type="button" className="notice-action" onClick={() => refetch(query.rulesVersion)}>
            Retry
          </button>
        </div>
      )}

      {load.kind === "ok" && load.summary.n === 0 && (
        <section className="panel">
          <h2>Nothing to evaluate yet</h2>
          <p className="muted">
            There are no processed claims with a human decision to evaluate for rules version{" "}
            <span className="mono">{load.summary.rules_version}</span>. Analyze some claims, or check the rules
            version.
          </p>
        </section>
      )}

      {load.kind === "ok" && load.summary.n > 0 && (
        <>
          <SummaryCards summary={load.summary} />
          <div className={selectedClaim ? "workspace with-detail" : "workspace"}>
            <ClaimsTable claims={claims} selected={selected} onSelect={setSelected} />
            {selectedClaim && (
              <ClaimDetailPanel
                key={selectedClaim.tracking_number}
                row={detailRow(selectedClaim)}
                onClose={() => setSelected(null)}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** The detail panel takes an Analyze row. An evaluated claim has finished: the orchestrator marks it
 * "done" when paid automatically and "needs_review" otherwise. */
function detailRow(claim: MetricsClaimRow): Row {
  return {
    tracking_number: claim.tracking_number,
    status: claim.route === "auto" ? "done" : "needs_review",
    queue_position: null,
    summary: null,
    failure_reason: null,
  };
}

function SummaryCards({ summary }: { summary: MetricsSummary }) {
  const { n, ai_mae, ai_bias, baseline_mae, baseline_description, improvement_pct } = summary;
  const improvementTone =
    improvement_pct === null || improvement_pct === 0 ? "" : improvement_pct > 0 ? "good" : "bad";

  return (
    <div className="metric-cards">
      <MetricCard title="Average AI error" value={whole(ai_mae)}>
        MAE · {n} {n === 1 ? "claim" : "claims"}
      </MetricCard>

      <MetricCard title="Simple rule error" value={whole(baseline_mae)}>
        <span className="mono">{baseline_description}</span>
      </MetricCard>

      <MetricCard
        title="Improvement"
        tone={improvementTone}
        value={
          improvement_pct === null
            ? "—"
            : `${Math.abs(improvement_pct).toFixed(1)}% ${improvement_pct >= 0 ? "less" : "more"} error`
        }
      >
        {ai_bias !== null && <div>The AI pays {signed(ai_bias, wholeUsd.format)} on average</div>}
        <div className="small">Negative: it pays less than the human.</div>
      </MetricCard>
    </div>
  );
}

function MetricCard({ title, value, tone = "", children }: { title: string; value: string; tone?: string; children: ReactNode }) {
  return (
    <section className="metric-card">
      <h2>{title}</h2>
      <div className={tone ? `metric-value metric-${tone}` : "metric-value"}>{value}</div>
      <div className="muted small">{children}</div>
    </section>
  );
}

type TableProps = {
  claims: MetricsClaimRow[];
  selected: string | null;
  onSelect: (tracking: string) => void;
};

function ClaimsTable({ claims, selected, onSelect }: TableProps) {
  return (
    <section className="panel">
      <div className="results-header">
        <h2>Claims</h2>
        <span className="muted small">Largest error first · click a row for the details</span>
      </div>
      <div className="table-wrap">
        <table className="results">
          <thead>
            <tr>
              <th>Tracking</th>
              <th className="num">Human</th>
              <th className="num">AI</th>
              <th className="num">Difference</th>
              <th>Route</th>
              <th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {claims.map((c) => (
              <tr
                key={c.tracking_number}
                className={c.tracking_number === selected ? "selected" : undefined}
                onClick={() => onSelect(c.tracking_number)}
                onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(c.tracking_number)}
                tabIndex={0}
                aria-selected={c.tracking_number === selected}
              >
                <td className="mono">{c.tracking_number}</td>
                <td className="num">{money(c.human)}</td>
                <td className="num">{money(c.ai)}</td>
                <td className={`num difference ${differenceTone(c.difference)}`}>
                  <strong>{signed(c.difference, money)}</strong>
                </td>
                <td>{ROUTES[c.route] ?? c.route}</td>
                <td>
                  {c.flags.length === 0
                    ? "—"
                    : c.flags.map((flag) => {
                        const info = flagInfo(flag);
                        return (
                          <span key={flag} className="tag" title={info.message}>
                            {info.label}
                          </span>
                        );
                      })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Difference = AI − human. Red: the AI pays less. Amber: it pays more.</p>
    </section>
  );
}

function differenceTone(difference: number): string {
  return difference < 0 ? "pays-less" : difference > 0 ? "pays-more" : "";
}

function MetricsSkeleton() {
  return (
    <div className="metrics-skeleton" aria-busy="true" aria-label="Loading metrics">
      <div className="metric-cards">
        {[0, 1, 2].map((i) => (
          <section key={i} className="metric-card">
            <div className="skeleton skeleton-title" />
            <div className="skeleton skeleton-value" />
            <div className="skeleton skeleton-line" />
          </section>
        ))}
      </div>
      <section className="panel skeleton-table">
        <div className="skeleton skeleton-title" />
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="skeleton skeleton-row" />
        ))}
      </section>
    </div>
  );
}
