"use client";

/**
 * usePersistedGrainState — a ChartGrain backed by localStorage.
 *
 * The initial render uses the SSR-safe default (so server and first client
 * render agree, avoiding a hydration mismatch); a post-mount effect hydrates
 * from localStorage. Updates write through synchronously. Each panel persists
 * independently under its own key, shared across every entity of that panel
 * type. Display timezone is governed globally (lib/preferences/timezone.ts),
 * not per panel; grain, like tz, adds no request parameter.
 */

import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_CHART_GRAIN,
  isChartGrain,
  type ChartGrain,
} from "@/lib/chart-grain";

/** Stable localStorage keys, one per chart-bearing surface type. */
export const GRAIN_KEYS = {
  validationResults: "grain:validation:results",
  governanceMetricResults: "grain:governance:metric-results",
  governanceDashboard: "grain:governance:dashboard",
} as const;

export interface UsePersistedGrainStateResult {
  grain: ChartGrain;
  setGrain: (g: ChartGrain) => void;
}

export function usePersistedGrainState(
  storageKey: string,
): UsePersistedGrainStateResult {
  const [grain, setGrainState] = useState<ChartGrain>(DEFAULT_CHART_GRAIN);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (isChartGrain(raw)) setGrainState(raw);
    } catch {
      // Ignore malformed/unavailable storage — fall back to the default.
    }
  }, [storageKey]);

  const setGrain = useCallback(
    (next: ChartGrain) => {
      setGrainState(next);
      try {
        localStorage.setItem(storageKey, next);
      } catch {
        // Ignore write failures (private mode, quota) — in-memory state holds.
      }
    },
    [storageKey],
  );

  return { grain, setGrain };
}
