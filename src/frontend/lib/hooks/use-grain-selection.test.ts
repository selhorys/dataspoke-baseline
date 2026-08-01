/**
 * Tests for lib/hooks/use-grain-selection.ts — localStorage-backed ChartGrain.
 *
 * Spec traces (spec/feature/FRONTEND_BASIC.md §Shared Component Notes →
 * ChartGrainPicker):
 *   - "It selects one of three grains — hourly, **daily (default)**, weekly".
 *   - "The selection **persists across visits** in browser `localStorage` under a
 *     stable key per logical panel, by the same rule as the RangePicker
 *     selection — each panel keeps its own grain, shared across all entities of
 *     that panel type."
 *   - "the grain is a client-side display concern and adds no request
 *     parameter" — so the hook's only side effect is storage.
 *
 * Mirrors lib/hooks/use-range-selection.test.ts: the initial render uses the
 * SSR-safe default and a post-mount useEffect hydrates from localStorage, so
 * hydration assertions wait for that effect.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { usePersistedGrainState, GRAIN_KEYS } from "./use-grain-selection";
import { CHART_GRAINS, DEFAULT_CHART_GRAIN } from "@/lib/chart-grain";

const KEY = "grain:test:panel";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("usePersistedGrainState — initial value", () => {
  it("is the documented default (daily) when storage is empty", () => {
    const { result } = renderHook(() => usePersistedGrainState(KEY));
    expect(result.current.grain).toBe(DEFAULT_CHART_GRAIN);
    expect(result.current.grain).toBe("daily");
  });
});

describe("usePersistedGrainState — hydration from storage", () => {
  it.each(CHART_GRAINS.map((g) => [g] as const))(
    "hydrates to a valid stored grain (%s) after mount",
    async (stored) => {
      localStorage.setItem(KEY, stored);

      const { result } = renderHook(() => usePersistedGrainState(KEY));

      await waitFor(() => {
        expect(result.current.grain).toBe(stored);
      });
    },
  );
});

describe("usePersistedGrainState — setter persistence", () => {
  it("setGrain updates state and writes the grain to localStorage", () => {
    const { result } = renderHook(() => usePersistedGrainState(KEY));

    act(() => {
      result.current.setGrain("weekly");
    });

    expect(result.current.grain).toBe("weekly");
    expect(localStorage.getItem(KEY)).toBe("weekly");
  });

  it("a written grain is what a later visit hydrates to (persists across visits)", async () => {
    const first = renderHook(() => usePersistedGrainState(KEY));
    act(() => {
      first.result.current.setGrain("hourly");
    });
    first.unmount();

    // A fresh mount stands in for the next visit to the same panel.
    const second = renderHook(() => usePersistedGrainState(KEY));
    await waitFor(() => {
      expect(second.result.current.grain).toBe("hourly");
    });
  });
});

describe("usePersistedGrainState — unusable stored value", () => {
  it.each(["yearly", "{", "", "Daily", "null"])(
    "ignores the stored value %j and stays at the default",
    async (stored) => {
      localStorage.setItem(KEY, stored);

      const { result } = renderHook(() => usePersistedGrainState(KEY));

      await act(async () => {
        await Promise.resolve();
      });
      expect(result.current.grain).toBe(DEFAULT_CHART_GRAIN);
    },
  );
});

describe("usePersistedGrainState — per-panel keys", () => {
  it("exposes a distinct stable key per chart-bearing panel type", () => {
    // spec: "a stable key per logical panel … each panel keeps its own grain".
    // The three chart surfaces named in the bullet are the governance dashboard,
    // the governance metric detail Result panel, and the per-dataset Validation
    // panel's Quality Score row.
    const keys = Object.values(GRAIN_KEYS);
    expect(keys).toHaveLength(3);
    expect(new Set(keys).size).toBe(3);
    expect(GRAIN_KEYS.validationResults).not.toBe(GRAIN_KEYS.governanceMetricResults);
    expect(GRAIN_KEYS.governanceDashboard).not.toBe(GRAIN_KEYS.governanceMetricResults);
  });

  it("persists two panels independently (no cross-contamination)", () => {
    const a = renderHook(() => usePersistedGrainState(GRAIN_KEYS.governanceDashboard));
    const b = renderHook(() => usePersistedGrainState(GRAIN_KEYS.validationResults));

    act(() => {
      a.result.current.setGrain("hourly");
    });
    act(() => {
      b.result.current.setGrain("weekly");
    });

    expect(a.result.current.grain).toBe("hourly");
    expect(b.result.current.grain).toBe("weekly");
    expect(localStorage.getItem(GRAIN_KEYS.governanceDashboard)).toBe("hourly");
    expect(localStorage.getItem(GRAIN_KEYS.validationResults)).toBe("weekly");
  });
});
