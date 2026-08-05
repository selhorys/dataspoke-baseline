"use client";

/**
 * usePersistedMetricViewState — a MetricViewState backed by localStorage.
 *
 * The initial render uses the SSR-safe default (so server and first client
 * render agree, avoiding a hydration mismatch); a post-mount effect hydrates
 * from localStorage. Updates write through synchronously, whichever field
 * changes. The persisted unit is the whole MetricViewState (types + search +
 * sort direction) under one key, so a surface restores its view in one read.
 * Each surface persists independently under its own key. Like the shared grain,
 * the view is display-only — it adds no request parameter.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_METRIC_VIEW,
  isMetricViewState,
  type MetricSortDir,
  type MetricViewState,
} from "@/lib/metric-view";
import type { MetricType } from "@/types/governance";

/** Stable localStorage keys, one per metric-view-bearing surface. */
export const METRIC_VIEW_KEYS = {
  governanceDashboard: "view:governance:dashboard",
} as const;

export interface UsePersistedMetricViewStateResult {
  view: MetricViewState;
  setTypes: (types: MetricType[]) => void;
  setSearch: (search: string) => void;
  setSortDir: (sortDir: MetricSortDir) => void;
}

export function usePersistedMetricViewState(
  storageKey: string,
): UsePersistedMetricViewStateResult {
  const [view, setViewState] = useState<MetricViewState>(() => ({
    ...DEFAULT_METRIC_VIEW,
    types: [...DEFAULT_METRIC_VIEW.types],
  }));

  // Mirrors the committed view so a patch merges onto the freshest value rather
  // than onto whatever the enclosing render closed over. Two setter calls in one
  // React batch would otherwise each merge onto the same stale view, and the
  // second would drop the first — including from the persisted copy.
  const viewRef = useRef<MetricViewState>(view);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const p: unknown = JSON.parse(raw);
        if (isMetricViewState(p)) {
          viewRef.current = p;
          setViewState(p);
        }
      }
    } catch {
      // Ignore malformed/unavailable storage — fall back to the default.
    }
  }, [storageKey]);

  const commit = useCallback(
    (patch: Partial<MetricViewState>) => {
      const next = { ...viewRef.current, ...patch };
      viewRef.current = next;
      setViewState(next);
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // Ignore write failures (private mode, quota) — in-memory state holds.
      }
    },
    [storageKey],
  );

  const setTypes = useCallback((types: MetricType[]) => commit({ types }), [commit]);
  const setSearch = useCallback((search: string) => commit({ search }), [commit]);
  const setSortDir = useCallback(
    (sortDir: MetricSortDir) => commit({ sortDir }),
    [commit],
  );

  return { view, setTypes, setSearch, setSortDir };
}
