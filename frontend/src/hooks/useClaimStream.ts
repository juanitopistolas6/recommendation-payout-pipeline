import { useEffect, useRef, useState } from "react";

import { streamUrl } from "../api/client";
import type { StreamClaimState } from "../api/types";

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "complete" | "error";

const RECONNECT_AFTER_TIMEOUT_MS = 1000;

/**
 * Opens an EventSource on /api/claims/stream for `ids` and calls `onClaim` with each state.
 *
 * - `complete`: closes it. If the client doesn't, EventSource reconnects on its own when the server hangs up.
 * - `timeout` (15 min on the server): closes and opens a new one; the initial snapshot means nothing is lost.
 * - Network errors: EventSource retries on its own; if it ends up CLOSED (e.g. a 400), reports `error`.
 *
 * A new `ids` array (even with the same IDs) restarts the stream; an empty one leaves it idle.
 */
export function useClaimStream(ids: string[], onClaim: (state: StreamClaimState) => void): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>("idle");
  const onClaimRef = useRef(onClaim);

  useEffect(() => {
    onClaimRef.current = onClaim;
  });

  useEffect(() => {
    if (ids.length === 0) return;

    let source: EventSource | null = null;
    let reconnectTimer: number | undefined;

    const connect = () => {
      setStatus("connecting");
      const es = new EventSource(streamUrl(ids));
      source = es;

      es.onopen = () => setStatus("open");
      es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "error" : "reconnecting");

      es.addEventListener("claim", (event) => {
        onClaimRef.current(JSON.parse((event as MessageEvent<string>).data) as StreamClaimState);
      });
      es.addEventListener("complete", () => {
        es.close();
        setStatus("complete");
      });
      es.addEventListener("timeout", () => {
        es.close();
        setStatus("reconnecting");
        reconnectTimer = window.setTimeout(connect, RECONNECT_AFTER_TIMEOUT_MS);
      });
    };

    connect();

    return () => {
      window.clearTimeout(reconnectTimer);
      source?.close();
    };
  }, [ids]);

  return ids.length === 0 ? "idle" : status;
}
