import { useEffect, useState, type ReactNode } from "react";

import { ApiError, getClaim } from "../api/client";
import { FINAL_STATUSES, type ClaimData, type ClaimDetail, type Recommendation } from "../api/types";
import {
  CATEGORIES,
  DOC_TYPES,
  ELIGIBILITY_SOURCES,
  PAYMENT_TYPES,
  ROUTES,
  STATUS,
  flagInfo,
  money,
  quotaResetTime,
  shortDate,
} from "../labels";
import type { Row } from "./ResultsTable";
import { StatusBadge } from "./StatusBadge";

type Load =
  | { kind: "loading" }
  | { kind: "ok"; detail: ClaimDetail }
  | { kind: "error"; message: string; notFound: boolean };

type Props = {
  row: Row;
  onClose: () => void;
};

/** Mount it with `key={tracking_number}`: switching claims discards the previous detail. */
export function ClaimDetailPanel({ row, onClose }: Props) {
  const { tracking_number: tracking, status } = row;
  const [load, setLoad] = useState<Load>({ kind: "loading" });

  // Refetched when the row's status changes (e.g. from "Analyzing" to "Done")
  useEffect(() => {
    if (status === "not_found") return;
    const controller = new AbortController();
    getClaim(tracking, controller.signal)
      .then((detail) => setLoad({ kind: "ok", detail }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const notFound = err instanceof ApiError && err.status === 404;
        setLoad({ kind: "error", message: err instanceof Error ? err.message : String(err), notFound });
      });
    return () => controller.abort();
  }, [tracking, status]);

  const detail = load.kind === "ok" ? load.detail : null;

  return (
    <aside className="detail" aria-label={`Claim ${tracking} details`}>
      <header className="detail-header">
        <div>
          <h2>
            Claim <span className="mono">{tracking}</span>
          </h2>
          {detail && <p className="muted">{address(detail.claim)}</p>}
        </div>
        <div className="detail-header-actions">
          <StatusBadge status={status} />
          <button type="button" className="ghost" onClick={onClose} aria-label="Close details">
            ✕
          </button>
        </div>
      </header>

      {status === "not_found" || (load.kind === "error" && load.notFound) ? (
        <Notice tone="danger" title="Not found">
          There's no claim with tracking number <span className="mono">{tracking}</span>. Check that it's spelled
          correctly and that the spreadsheet has been imported.
        </Notice>
      ) : load.kind === "error" ? (
        <Notice tone="danger" title="Couldn't load the details">
          {load.message}
        </Notice>
      ) : !detail ? (
        <p className="muted">Loading…</p>
      ) : (
        <DetailBody detail={detail} row={row} />
      )}
    </aside>
  );
}

function DetailBody({ detail, row }: { detail: ClaimDetail; row: Row }) {
  const { status } = row;
  const reason = row.failure_reason ?? detail.failure_reason;
  const rec = detail.recommendation;

  return (
    <>
      {status === "failed" && (
        <Notice tone="danger" title="Processing failed">
          <p className="pre">{reason || "No reason recorded."}</p>
          <p>
            A failed claim isn't retried automatically: check <strong>Force reprocessing</strong> and analyze it again.
          </p>
        </Notice>
      )}
      {status === "quota_exhausted" && <QuotaNotice reason={reason} />}
      {!FINAL_STATUSES.has(status) && (
        <Notice tone="progress" title={STATUS[status].label}>
          {status === "queued" && row.queue_position != null
            ? `Position ${row.queue_position} in the queue. `
            : "Processing. "}
          The details refresh automatically when it finishes.
        </Notice>
      )}

      {rec ? (
        <RecommendationView rec={rec} maxBenefit={detail.claim.max_benefit} />
      ) : (
        FINAL_STATUSES.has(status) && <p className="muted">This claim has no recommendation yet.</p>
      )}

      <section>
        <h3>Claim data</h3>
        <ClaimFacts claim={detail.claim} />
      </section>

      <section>
        <h3>Documents ({detail.documents.length})</h3>
        {detail.documents.length === 0 ? (
          <p className="muted">No documents registered.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Type</th>
                  <th className="num">Pages</th>
                </tr>
              </thead>
              <tbody>
                {detail.documents.map((doc) => (
                  <tr key={doc.id}>
                    <td className="break">{doc.file_name}</td>
                    <td>
                      {DOC_TYPES[doc.doc_type] ?? doc.doc_type}
                      {doc.has_scanned_pages && <span className="tag">scanned</span>}
                    </td>
                    <td className="num">{doc.page_count ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function RecommendationView({ rec, maxBenefit }: { rec: Recommendation; maxBenefit: string | null }) {
  const findings = Array.isArray(rec.findings) ? rec.findings : null;

  return (
    <>
      <section className="amount-card">
        <div>
          <div className="muted small">Recommended amount</div>
          <div className="amount">{money(rec.recommended_amount)}</div>
          {rec.cap_applied && <div className="cap-note">Cap applied: the max benefit is {money(maxBenefit)}</div>}
        </div>
        <dl className="facts compact">
          <dt>Route</dt>
          <dd>{ROUTES[rec.route] ?? rec.route}</dd>
          <dt>Eligible owed</dt>
          <dd>{money(rec.eligible_owed)}</dd>
          <dt>Excluded</dt>
          <dd>{money(rec.excluded_total)}</dd>
          <dt>Confidence</dt>
          <dd>{rec.extraction_confidence || "—"}</dd>
        </dl>
      </section>

      <section>
        <h3>Justification</h3>
        <p className="pre">{rec.explanation}</p>
        <p className="muted small">
          Model {rec.model_used} · rules {rec.rules_version} · {shortDate(rec.created_at)}
        </p>
      </section>

      <section>
        <h3>Flags ({rec.flags.length})</h3>
        {rec.flags.length === 0 ? (
          <p className="muted">No flags.</p>
        ) : (
          <ul className="flags">
            {rec.flags.map((flag) => {
              const info = flagInfo(flag);
              return (
                <li key={flag} className={info.critical ? "flag flag-critical" : "flag"}>
                  <strong>{info.label}</strong>
                  <span>{info.message}</span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section>
        <h3>Charges ({rec.charges.length})</h3>
        {rec.charges.length === 0 ? (
          <p className="muted">No charges were extracted.</p>
        ) : (
          <div className="table-wrap">
            <table className="charges">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Description</th>
                  <th className="num">Amount</th>
                  <th className="num">Balance</th>
                  <th>Category</th>
                  <th>Eligible</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {rec.charges.map((charge, i) => (
                  <tr key={i} className={charge.eligible ? undefined : "excluded"}>
                    <td className="nowrap">{shortDate(charge.date)}</td>
                    <td>
                      {charge.description}
                      {charge.source_document && <div className="muted small break">{charge.source_document}</div>}
                    </td>
                    <td className="num">{money(charge.amount)}</td>
                    <td className="num">{money(charge.balance)}</td>
                    <td>{charge.category ? CATEGORIES[charge.category] ?? charge.category : "—"}</td>
                    <td>
                      <span className={charge.eligible ? "pill pill-yes" : "pill pill-no"}>
                        {charge.eligible ? "Eligible" : "Excluded"}
                      </span>
                      {charge.eligibility_source && (
                        <div className="muted small">{ELIGIBILITY_SOURCES[charge.eligibility_source]}</div>
                      )}
                    </td>
                    <td>
                      {charge.reason}
                      {charge.lease_reference && <div className="muted small">Lease: {charge.lease_reference}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h3>Payments and credits ({rec.payments.length})</h3>
        {rec.payments.length === 0 ? (
          <p className="muted">No payments or credits.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Description</th>
                  <th>Type</th>
                  <th className="num">Amount</th>
                </tr>
              </thead>
              <tbody>
                {rec.payments.map((p, i) => (
                  <tr key={i}>
                    <td className="nowrap">{shortDate(p.date)}</td>
                    <td>{p.description}</td>
                    <td>{PAYMENT_TYPES[p.type] ?? p.type}</td>
                    <td className="num">{money(p.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {rec.findings != null && (
        <section>
          <h3>LLM findings</h3>
          {findings ? (
            findings.length === 0 ? (
              <p className="muted">No findings.</p>
            ) : (
              <ul className="findings">
                {findings.map((f, i) => (
                  <li key={i}>{typeof f === "string" ? f : JSON.stringify(f)}</li>
                ))}
              </ul>
            )
          ) : (
            <pre className="json">{JSON.stringify(rec.findings, null, 2)}</pre>
          )}
        </section>
      )}
    </>
  );
}

function ClaimFacts({ claim }: { claim: ClaimData }) {
  const facts: [string, string][] = [
    ["Claim amount", money(claim.claim_amount)],
    ["Max benefit", money(claim.max_benefit)],
    ["Monthly rent", money(claim.monthly_rent)],
    ["Claim date", shortDate(claim.claim_date)],
    ["Lease", `${shortDate(claim.lease_start_date)} → ${shortDate(claim.lease_end_date)}`],
    ["Move-out", shortDate(claim.move_out_date)],
    ["Termination", claim.termination_type || "—"],
    ["Property manager", [claim.property_management_company, claim.property_manager_name].filter(Boolean).join(" · ") || "—"],
  ];
  return (
    <>
      <dl className="facts">
        {facts.map(([k, v]) => (
          <div key={k} className="fact">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {claim.pm_explanation && (
        <>
          <h4>Property manager's explanation</h4>
          <p className="pre">{claim.pm_explanation}</p>
        </>
      )}
    </>
  );
}

function QuotaNotice({ reason }: { reason: string | null }) {
  const reset = quotaResetTime(reason);
  return (
    <Notice tone="quota" title="Daily Gemini quota exhausted">
      <p>
        {reset ? (
          <>
            Resets <strong>{reset}</strong> (midnight Pacific time).
          </>
        ) : (
          "Resets at midnight Pacific time."
        )}{" "}
        After that, analyze this claim again: it's re-queued without needing to force it.
      </p>
      {reason && <p className="muted small">{reason}</p>}
    </Notice>
  );
}

function Notice({ tone, title, children }: { tone: string; title: string; children: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      <strong>{title}</strong>
      <div>{children}</div>
    </div>
  );
}

function address(claim: ClaimData) {
  const cityLine = [claim.lease_city, [claim.lease_state, claim.lease_zip].filter(Boolean).join(" ")]
    .filter(Boolean)
    .join(", ");
  return [claim.lease_street_address, cityLine].filter(Boolean).join(", ") || "No address";
}
