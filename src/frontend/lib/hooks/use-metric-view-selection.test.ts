/**
 * Tests for lib/hooks/use-metric-view-selection.ts — the localStorage-backed
 * MetricViewState behind the governance dashboard's view controls.
 *
 * Spec traces (spec/feature/FRONTEND_GOVERNANCE.md §Dashboard → "Metric view
 * controls"):
 *   - the type filter is "all selected by default", the title search is
 *     "inactive while blank", and the title sort is "ascending by
 *     default" — so an unseeded hook must return every metric type, an empty
 *     search, and the ascending direction;
 *   - "Each selection persists across visits in browser `localStorage` under a
 *     stable key, by the same rule as the shared RangePicker and
 *     ChartGrainPicker selections";
 *   - the controls run over "the same GET /spoke/governance/metric … read —
 *     **no request parameter**", so the hook's only side effect is storage.
 *
 * Mirrors lib/hooks/use-grain-selection.test.ts: the initial render uses the
 * SSR-safe default and a post-mount useEffect hydrates from localStorage, so
 * hydration assertions wait for that effect. The malformed-payload block matters
 * most — the guard gates untrusted localStorage input.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  usePersistedMetricViewState,
  METRIC_VIEW_KEYS,
} from "./use-metric-view-selection";
import { DEFAULT_METRIC_VIEW } from "@/lib/metric-view";
import { METRIC_TYPES } from "@/types/governance";

const KEY = "view:test:panel";

/**
 * The spec'd default view, written out as literals rather than reused from the
 * impl's DEFAULT_METRIC_VIEW / METRIC_TYPES. Comparing an impl constant with
 * itself cannot notice a dropped metric type, and cannot notice a leaked array
 * reference either (a caller's `push` would mutate both sides of the compare).
 *
 * spec: FRONTEND_GOVERNANCE.md §Dashboard — the type filter is over "the built-in
 *   `metric_type` values listed in USE_CASE §UC5 … all selected by default", the
 *   title search is "inactive while blank", the sort is "ascending by
 *   default".
 * spec: USE_CASE_en.md §UC5 §Built-in active metric types — ingestion-freshness,
 *   validation-score, doc-health.
 */
const SPEC_DEFAULT_VIEW = {
  types: ["ingestion-freshness", "validation-score", "doc-health"],
  search: "",
  sortDir: "asc",
};

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("usePersistedMetricViewState — initial value", () => {
  it("is the documented default (every type, blank search, ascending) when storage is empty", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    // Compared against the spec'd literals, so dropping a metric type from the
    // impl's constant fails here rather than silently redefining "default".
    expect(result.current.view).toEqual(SPEC_DEFAULT_VIEW);
  });

  it("hands back a types array the caller cannot use to mutate the default", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    result.current.view.types.push("doc-health");

    // DEFAULT_METRIC_VIEW is module-level shared state; a leaked reference would
    // corrupt every later mount in the same tab. The expected side is the test's
    // own literal — comparing the leaked array against METRIC_TYPES would let the
    // `push` above mutate both sides and pass regardless.
    expect(DEFAULT_METRIC_VIEW.types).toEqual(SPEC_DEFAULT_VIEW.types);
    // DEFAULT_METRIC_VIEW.types is itself a copy of METRIC_TYPES, so guarding
    // only the former still lets a hook that leaked METRIC_TYPES pass.
    expect(METRIC_TYPES).toEqual(SPEC_DEFAULT_VIEW.types);
  });
});

describe("usePersistedMetricViewState — hydration from storage", () => {
  it("hydrates a valid stored view after mount", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ types: ["doc-health"], search: "freshness", sortDir: "desc" }),
    );

    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    await waitFor(() => {
      expect(result.current.view).toEqual({
        types: ["doc-health"],
        search: "freshness",
        sortDir: "desc",
      });
    });
  });

  it("hydrates an empty type selection as empty (no fallback to all)", async () => {
    // spec: "deselecting every type yields an empty set rather than falling back
    // to all" — that has to survive the storage round-trip too.
    localStorage.setItem(
      KEY,
      JSON.stringify({ types: [], search: "", sortDir: "asc" }),
    );

    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    await waitFor(() => {
      expect(result.current.view.types).toEqual([]);
    });
  });
});

describe("usePersistedMetricViewState — setter persistence", () => {
  it("setTypes updates state and writes through to localStorage", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    act(() => {
      result.current.setTypes(["validation-score", "doc-health"]);
    });

    expect(result.current.view.types).toEqual(["validation-score", "doc-health"]);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      types: ["validation-score", "doc-health"],
      search: "",
      sortDir: "asc",
    });
  });

  it("setSearch updates state and writes through to localStorage", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    act(() => {
      result.current.setSearch("coverage");
    });

    expect(result.current.view.search).toBe("coverage");
    expect(JSON.parse(localStorage.getItem(KEY) as string).search).toBe("coverage");
  });

  it("setSortDir updates state and writes through to localStorage", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    act(() => {
      result.current.setSortDir("desc");
    });

    expect(result.current.view.sortDir).toBe("desc");
    expect(JSON.parse(localStorage.getItem(KEY) as string).sortDir).toBe("desc");
  });

  it("each setter leaves the other two fields intact", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    act(() => {
      result.current.setTypes(["doc-health"]);
    });
    act(() => {
      result.current.setSearch("doc");
    });
    act(() => {
      result.current.setSortDir("desc");
    });

    expect(result.current.view).toEqual({
      types: ["doc-health"],
      search: "doc",
      sortDir: "desc",
    });
  });

  it("a written view is what a later visit hydrates to (persists across visits)", async () => {
    const first = renderHook(() => usePersistedMetricViewState(KEY));
    act(() => {
      first.result.current.setTypes(["ingestion-freshness"]);
    });
    act(() => {
      first.result.current.setSortDir("desc");
    });
    first.unmount();

    // A fresh mount stands in for the next visit to the same surface.
    const second = renderHook(() => usePersistedMetricViewState(KEY));
    await waitFor(() => {
      expect(second.result.current.view).toEqual({
        types: ["ingestion-freshness"],
        search: "",
        sortDir: "desc",
      });
    });
  });

  it("composes two setters fired in one batch, in state and in storage", () => {
    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    // Both calls land in a single React batch, so each patch must merge onto the
    // freshest view rather than onto the render's captured one.
    act(() => {
      result.current.setSearch("doc");
      result.current.setSortDir("desc");
    });

    expect(result.current.view.search).toBe("doc");
    expect(result.current.view.sortDir).toBe("desc");
    // The whole view, not a subset: a batch that dropped `types` from the
    // persisted copy would satisfy a partial match.
    expect(JSON.parse(localStorage.getItem(KEY) ?? "{}")).toEqual({
      types: SPEC_DEFAULT_VIEW.types,
      search: "doc",
      sortDir: "desc",
    });
  });
});

describe("usePersistedMetricViewState — unusable stored value", () => {
  it.each([
    ["not JSON at all", "{"],
    ["a bare string", '"asc"'],
    ["null", "null"],
    ["an array", "[]"],
    ["a view missing search", '{"types":["doc-health"],"sortDir":"asc"}'],
    ["a view missing sortDir", '{"types":["doc-health"],"search":""}'],
    ["a view missing types", '{"search":"","sortDir":"asc"}'],
    ["a non-string search", '{"types":[],"search":3,"sortDir":"asc"}'],
    ["an unknown sort direction", '{"types":[],"search":"","sortDir":"ascending"}'],
    ["an unknown metric type", '{"types":["cost-health"],"search":"","sortDir":"asc"}'],
    ["a non-array types", '{"types":"doc-health","search":"","sortDir":"asc"}'],
  ])("ignores %s and stays at the default", async (_label, stored) => {
    localStorage.setItem(KEY, stored);

    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.view).toEqual(SPEC_DEFAULT_VIEW);
  });

  it("still hydrates a valid payload (the rejection block is not vacuous)", async () => {
    // Backstop for the table above: hydration works, so those cases really are
    // being rejected by the guard rather than never read at all.
    localStorage.setItem(
      KEY,
      JSON.stringify({ types: ["doc-health"], search: "", sortDir: "asc" }),
    );

    const { result } = renderHook(() => usePersistedMetricViewState(KEY));

    await waitFor(() => {
      expect(result.current.view.types).toEqual(["doc-health"]);
    });
  });
});

describe("usePersistedMetricViewState — per-surface keys", () => {
  it("exposes a stable key for the governance dashboard", () => {
    expect(METRIC_VIEW_KEYS.governanceDashboard).toBeTruthy();
    // Guard for when a second metric-view surface is added: two surfaces sharing
    // one key would silently overwrite each other. Trivially true today (one
    // entry) — it only starts carrying signal with the second key.
    expect(new Set(Object.values(METRIC_VIEW_KEYS)).size).toBe(
      Object.values(METRIC_VIEW_KEYS).length,
    );
  });

  it("persists two surfaces independently (no cross-contamination)", () => {
    const a = renderHook(() =>
      usePersistedMetricViewState(METRIC_VIEW_KEYS.governanceDashboard),
    );
    const b = renderHook(() => usePersistedMetricViewState(KEY));

    act(() => {
      a.result.current.setSearch("dashboard-needle");
    });
    act(() => {
      b.result.current.setSearch("other-needle");
    });

    expect(a.result.current.view.search).toBe("dashboard-needle");
    expect(b.result.current.view.search).toBe("other-needle");

    // In-memory state alone proves nothing here — each hook instance owns its own
    // useState regardless of the key. What "under a stable key, per surface" means
    // is that the two WRITES land under the two distinct keys, so read storage back.
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — "Each selection persists across
    //   visits in browser `localStorage` under a stable key".
    expect(
      JSON.parse(
        localStorage.getItem(METRIC_VIEW_KEYS.governanceDashboard) as string,
      ).search,
    ).toBe("dashboard-needle");
    expect(JSON.parse(localStorage.getItem(KEY) as string).search).toBe("other-needle");
  });
});
