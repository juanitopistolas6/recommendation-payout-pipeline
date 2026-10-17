import type { ClaimStatus, Summary } from "../api/types";
import { ROUTES, money, quotaResetTime } from "../labels";
import { StatusBadge } from "./StatusBadge";

export type Row = {
  tracking_number: string;
  status: ClaimStatus;
  queue_position: number | null;
  summary: Summary | null;
  failure_reason: string | null;
};

type Props = {
  rows: Row[];
  selected: string | null;
  onSelect: (trackingNumber: string) => void;
};

export function ResultsTable({ rows, selected, onSelect }: Props) {
  return (
    <div className="table-wrap">
      <table className="results">
        <thead>
          <tr>
            <th>Tracking</th>
            <th>Status</th>
            <th className="num">Recommended</th>
            <th>Route</th>
            <th className="num">Flags</th>
            <th className="num">Queue</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.tracking_number}
              className={row.tracking_number === selected ? "selected" : undefined}
              onClick={() => onSelect(row.tracking_number)}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(row.tracking_number)}
              tabIndex={0}
              aria-selected={row.tracking_number === selected}
            >
              <td className="mono">{row.tracking_number}</td>
              <td>
                <StatusBadge status={row.status} />
                <RowNote row={row} />
              </td>
              <td className="num">{money(row.summary?.recommended_amount)}</td>
              <td>{row.summary ? ROUTES[row.summary.route] ?? row.summary.route : "—"}</td>
              <td className="num">{row.summary ? row.summary.flags.length : "—"}</td>
              <td className="num">
                {row.status === "queued" && row.queue_position != null ? `#${row.queue_position}` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RowNote({ row }: { row: Row }) {
  if (row.status === "quota_exhausted") {
    const reset = quotaResetTime(row.failure_reason);
    return <div className="row-note">{reset ? `Resets ${reset}` : "Resets at midnight Pacific time"}</div>;
  }
  if (row.status === "failed" && row.failure_reason) {
    return (
      <div className="row-note row-note-error" title={row.failure_reason}>
        {row.failure_reason}
      </div>
    );
  }
  return null;
}
