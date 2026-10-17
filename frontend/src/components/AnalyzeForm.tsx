import { useState, type FormEvent, type KeyboardEvent } from "react";

import { parseIds } from "../labels";

export const MAX_IDS = 15; // MAX_IDS in backend/claims/api.py

type Props = {
  submitting: boolean;
  onSubmit: (ids: string[], force: boolean) => void;
};

export function AnalyzeForm({ submitting, onSubmit }: Props) {
  const [raw, setRaw] = useState("");
  const [force, setForce] = useState(false);

  const ids = parseIds(raw);
  const tooMany = ids.length > MAX_IDS;
  const disabled = submitting || ids.length === 0 || tooMany;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!disabled) onSubmit(ids, force);
  };

  // Ctrl/Cmd + Enter submits from the textarea
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
  };

  return (
    <form className="analyze-form" onSubmit={submit}>
      <label htmlFor="ids">Tracking numbers</label>
      <textarea
        id="ids"
        rows={3}
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="1, 10, 100 — separated by commas, spaces or new lines"
        spellCheck={false}
      />
      <div className="analyze-form-actions">
        <label className="checkbox">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Force reprocessing
        </label>
        <span className={tooMany ? "hint hint-error" : "hint"}>
          {tooMany ? `Up to ${MAX_IDS} IDs (got ${ids.length})` : `${ids.length} ${ids.length === 1 ? "ID" : "IDs"}`}
        </span>
        <button type="submit" disabled={disabled}>
          {submitting ? "Sending…" : "Analyze"}
        </button>
      </div>
    </form>
  );
}
