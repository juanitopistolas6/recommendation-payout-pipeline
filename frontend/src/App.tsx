import { useCallback, useEffect, useRef, useState } from "react";

import { analyzeClaims } from "./api/client";
import { FINAL_STATUSES, type AnalyzeItem, type StreamClaimState } from "./api/types";
import { AnalyzeForm } from "./components/AnalyzeForm";
import { ClaimDetailPanel } from "./components/ClaimDetailPanel";
import { MetricsView } from "./components/MetricsView";
import { ResultsTable, type Row } from "./components/ResultsTable";
import { useClaimStream, type StreamStatus } from "./hooks/useClaimStream";

const STREAM_LABELS: Record<StreamStatus, string> = {
  idle: "",
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
  complete: "Complete",
  error: "Live updates unavailable",
};

type Tab = "analyze" | "metrics";

// The tab lives in the URL hash (#metrics) so a reload or a shared link opens the same view
function tabFromHash(): Tab {
  return window.location.hash === "#metrics" ? "metrics" : "analyze";
}

function useTab(): [Tab, (tab: Tab) => void] {
  const [tab, setTab] = useState<Tab>(tabFromHash);

  useEffect(() => {
    const onHashChange = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const select = (next: Tab) => {
    window.location.hash = next === "metrics" ? "metrics" : "";
    setTab(next);
  };
  return [tab, select];
}

function rowFromAnalyze(item: AnalyzeItem): Row {
  return {
    tracking_number: item.tracking_number,
    status: item.status,
    queue_position: item.queue_position ?? null,
    summary: item.summary ?? null,
    failure_reason: item.failure_reason ?? null,
  };
}

function mergeStreamState(row: Row, state: StreamClaimState): Row {
  return {
    ...row,
    status: state.status,
    queue_position: state.queue_position ?? null,
    summary: state.summary ?? null,
    failure_reason: state.failure_reason ?? null,
  };
}

function App() {
  const [rows, setRows] = useState<Row[]>([]);
  const [streamIds, setStreamIds] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  const [tab, setTab] = useTab();

  const onClaim = useCallback((state: StreamClaimState) => {
    setRows((current) =>
      current.map((row) => (row.tracking_number === state.tracking_number ? mergeStreamState(row, state) : row)),
    );
  }, []);

  const streamStatus = useClaimStream(streamIds, onClaim);

  const analyze = async (ids: string[], force: boolean) => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSubmitting(true);
    setError(null);
    try {
      const items = await analyzeClaims(ids, force, controller.signal);
      setRows(items.map(rowFromAnalyze));
      // not_found and finished claims render right away; only the rest are followed live
      setStreamIds(items.filter((item) => !FINAL_STATUSES.has(item.status)).map((item) => item.tracking_number));
      setSelected((current) => (current && items.some((i) => i.tracking_number === current) ? current : null));
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (pending.current === controller) setSubmitting(false);
    }
  };

  const selectedRow = rows.find((row) => row.tracking_number === selected) ?? null;
  const finished = rows.filter((row) => FINAL_STATUSES.has(row.status)).length;

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Corgi Claims</h1>
          <p className="muted">Recommended payouts for security deposit claims, based on their documents.</p>
        </div>
        <nav className="tabs" aria-label="Sections">
          {(["analyze", "metrics"] as const).map((t) => (
            <button
              key={t}
              type="button"
              className={tab === t ? "tab tab-active" : "tab"}
              aria-current={tab === t ? "page" : undefined}
              onClick={() => setTab(t)}
            >
              {t === "analyze" ? "Analyze" : "Metrics"}
            </button>
          ))}
        </nav>
      </header>

      {tab === "metrics" && <MetricsView />}

      {/* Hidden rather than unmounted: results and the live stream survive a visit to Metrics */}
      <div className="analyze-view" hidden={tab !== "analyze"}>
        <AnalyzeForm submitting={submitting} onSubmit={analyze} />

        {error && (
          <div className="notice notice-danger" role="alert">
            <strong>Couldn't analyze</strong>
            <div>{error}</div>
          </div>
        )}

        {rows.length > 0 && (
          <div className={selectedRow ? "workspace with-detail" : "workspace"}>
            <section className="results-section">
              <div className="results-header">
                <h2>Results</h2>
                <span className="muted small">
                  {finished}/{rows.length} finished
                </span>
                {streamStatus !== "idle" && (
                  <span className={`stream stream-${streamStatus}`}>{STREAM_LABELS[streamStatus]}</span>
                )}
              </div>
              <ResultsTable rows={rows} selected={selected} onSelect={setSelected} />
              <p className="muted small">Click a row to see the details.</p>
            </section>

            {selectedRow && (
              <ClaimDetailPanel key={selectedRow.tracking_number} row={selectedRow} onClose={() => setSelected(null)} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
