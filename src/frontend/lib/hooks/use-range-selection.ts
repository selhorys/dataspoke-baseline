"use client";

/**
 * usePersistedRangeState — a {selection, tz} unit backed by localStorage.
 *
 * The initial render uses the SSR-safe default (so server and first client
 * render agree, avoiding a hydration mismatch); a post-mount effect hydrates
 * from localStorage. Updates write through synchronously. The persisted unit is
 * { selection, tz }: the selection (intent — not resolved bounds, so a stored
 * "Last 7 days" keeps tracking the present) plus the timezone interpretation.
 * tz rides inside the same stored object, so it remains per-picker/per-panel and
 * independent across surfaces.
 */

import { useCallback, useEffect, useState } from "react";
import {
  defaultSelection,
  isRangeSelection,
  type RangeSelection,
  type TzMode,
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

/** Persisted unit: range intent + timezone interpretation. */
export interface RangeState {
  selection: RangeSelection;
  tz: TzMode;
}

/** Default state: the documented default selection, local timezone. */
function defaultState(): RangeState {
  return { selection: defaultSelection(), tz: "local" };
}

/** Shape guard for the persisted {selection, tz} unit. */
export function isRangeState(x: unknown): x is RangeState {
  if (typeof x !== "object" || x === null) return false;
  const v = x as Record<string, unknown>;
  if (v.tz !== "local" && v.tz !== "utc") return false;
  return isRangeSelection(v.selection);
}

export interface UsePersistedRangeStateResult {
  selection: RangeSelection;
  tz: TzMode;
  setSelection: (s: RangeSelection) => void;
  setTz: (tz: TzMode) => void;
}

export function usePersistedRangeState(
  storageKey: string,
): UsePersistedRangeStateResult {
  const [state, setState] = useState<RangeState>(() => defaultState());

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const p: unknown = JSON.parse(raw);
        if (isRangeState(p)) setState(p);
      }
    } catch {
      // Ignore malformed/unavailable storage — fall back to the default.
    }
  }, [storageKey]);

  // Single write-through helper applied to a functional update of either field,
  // so concurrent selection/tz updates don't clobber each other.
  const patch = useCallback(
    (apply: (prev: RangeState) => RangeState) => {
      setState((prev) => {
        const next = apply(prev);
        try {
          localStorage.setItem(storageKey, JSON.stringify(next));
        } catch {
          // Ignore write failures (private mode, quota) — in-memory state holds.
        }
        return next;
      });
    },
    [storageKey],
  );

  const setSelection = useCallback(
    (selection: RangeSelection) => patch((prev) => ({ ...prev, selection })),
    [patch],
  );

  const setTz = useCallback(
    (tz: TzMode) => patch((prev) => ({ ...prev, tz })),
    [patch],
  );

  return { selection: state.selection, tz: state.tz, setSelection, setTz };
}
