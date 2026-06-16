"use client";

/**
 * usePersistedRangeState — a RangeSelection backed by localStorage.
 *
 * The initial render uses the SSR-safe default (so server and first client
 * render agree, avoiding a hydration mismatch); a post-mount effect hydrates
 * from localStorage. Updates write through synchronously. The persisted unit is
 * the RangeSelection (intent — not resolved bounds, so a stored "Last 7 days"
 * keeps tracking the present). Each panel persists independently under its own
 * key. Display timezone is governed globally (lib/preferences/timezone.ts), not
 * per panel.
 */

import { useCallback, useEffect, useState } from "react";
import {
  defaultSelection,
  isRangeSelection,
  type RangeSelection,
} from "@/lib/range";

/** Stable localStorage keys, one per range-bearing surface type. */
export const RANGE_KEYS = {
  validationResults: "range:validation:results",
  validationEvents: "range:validation:events",
  governanceMetricResults: "range:governance:metric-results",
  governanceMetricEvents: "range:governance:metric-events",
  governanceDashboard: "range:governance:dashboard",
  ingestionSourceEvents: "range:ingestion:source-events",
  ingestionDatasetEvents: "range:ingestion:dataset-events",
} as const;

export interface UsePersistedRangeStateResult {
  selection: RangeSelection;
  setSelection: (s: RangeSelection) => void;
}

export function usePersistedRangeState(
  storageKey: string,
): UsePersistedRangeStateResult {
  const [selection, setSelectionState] = useState<RangeSelection>(() =>
    defaultSelection(),
  );

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const p: unknown = JSON.parse(raw);
        if (isRangeSelection(p)) setSelectionState(p);
      }
    } catch {
      // Ignore malformed/unavailable storage — fall back to the default.
    }
  }, [storageKey]);

  const setSelection = useCallback(
    (next: RangeSelection) => {
      setSelectionState(next);
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // Ignore write failures (private mode, quota) — in-memory state holds.
      }
    },
    [storageKey],
  );

  return { selection, setSelection };
}
