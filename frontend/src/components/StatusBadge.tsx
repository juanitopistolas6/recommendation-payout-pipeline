import type { ClaimStatus } from "../api/types";
import { STATUS } from "../labels";

export function StatusBadge({ status }: { status: ClaimStatus }) {
  const { label, tone } = STATUS[status] ?? { label: status, tone: "neutral" };
  return <span className={`badge badge-${tone}`}>{label}</span>;
}
