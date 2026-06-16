/**
 * Tests for lib/hooks/use-range-selection.ts — localStorage-backed
 * {selection, tz} unit.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "The selection persists across visits in browser localStorage under a
 *     stable key per logical panel — each panel persists independently … so
 *     revisiting a panel restores the last-used selection." Default is the
 *     2-week preset.
 *   - lib/range.ts: persisted value is the RangeSelection (intent) plus the
 *     TzMode interpretation, guarded by isRangeState on read; corrupt/unavailable
 *     storage falls back to the default { selection, tz: "local" }.
 *
 * The initial render uses the SSR-safe default; a post-mount useEffect hydrates
 * from localStorage, so hydration assertions wait for that effect.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  usePersistedRangeState,
  isRangeState,
  type RangeState,
} from "./use-range-selection";
import { defaultSelection, type RangeSelection } from "@/lib/range";

const KEY = "range:test:panel";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("usePersistedRangeState — initial value", () => {
  it("is the documented default (2-week preset, local tz) when storage is empty", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));
    expect(result.current.selection).toEqual(defaultSelection());
    expect(result.current.selection).toEqual({ kind: "preset", days: 14 });
    expect(result.current.tz).toBe("local");
  });
});

describe("usePersistedRangeState — hydration from storage", () => {
  it("hydrates to a valid stored {selection, tz} after mount", async () => {
    const stored: RangeState = {
      selection: { kind: "preset", days: 7 },
      tz: "utc",
    };
    localStorage.setItem(KEY, JSON.stringify(stored));

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await waitFor(() => {
      expect(result.current.selection).toEqual(stored.selection);
      expect(result.current.tz).toBe("utc");
    });
  });

  it("hydrates to a valid stored custom selection after mount", async () => {
    const stored: RangeState = {
      selection: {
        kind: "custom",
        from: "2024-03-01T00:00:00.000Z",
        to: "2024-03-05T23:59:59.999Z",
      },
      tz: "local",
    };
    localStorage.setItem(KEY, JSON.stringify(stored));

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await waitFor(() => {
      expect(result.current.selection).toEqual(stored.selection);
    });
  });
});

describe("usePersistedRangeState — setter persistence", () => {
  it("setSelection updates state and writes the unit to localStorage", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));

    const next: RangeSelection = { kind: "preset", days: 28 };
    act(() => {
      result.current.setSelection(next);
    });

    expect(result.current.selection).toEqual(next);
    const raw = localStorage.getItem(KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string)).toEqual({ selection: next, tz: "local" });
  });

  it("setTz updates the tz and preserves the selection", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));

    act(() => {
      result.current.setTz("utc");
    });

    expect(result.current.tz).toBe("utc");
    expect(result.current.selection).toEqual(defaultSelection());
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      selection: defaultSelection(),
      tz: "utc",
    });
  });

  it("persists a custom selection through setSelection", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));

    const next: RangeSelection = {
      kind: "custom",
      from: "2024-01-10T00:00:00.000Z",
      to: "2024-01-12T23:59:59.999Z",
    };
    act(() => {
      result.current.setSelection(next);
    });

    expect(result.current.selection).toEqual(next);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      selection: next,
      tz: "local",
    });
  });
});

describe("usePersistedRangeState — corrupt storage", () => {
  it("ignores invalid JSON and stays at the default (no throw)", async () => {
    localStorage.setItem(KEY, "{not valid json");

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.selection).toEqual(defaultSelection());
    expect(result.current.tz).toBe("local");
  });

  it("ignores well-formed JSON that fails the shape guard", async () => {
    // Missing tz field — isRangeState rejects it.
    localStorage.setItem(
      KEY,
      JSON.stringify({ selection: { kind: "preset", days: 9 } }),
    );

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.selection).toEqual(defaultSelection());
  });
});

describe("usePersistedRangeState — key isolation", () => {
  it("persists two different keys independently (no cross-contamination)", () => {
    const keyA = "range:test:panel-a";
    const keyB = "range:test:panel-b";

    const a = renderHook(() => usePersistedRangeState(keyA));
    const b = renderHook(() => usePersistedRangeState(keyB));

    act(() => {
      a.result.current.setSelection({ kind: "preset", days: 1 });
      a.result.current.setTz("utc");
    });
    act(() => {
      b.result.current.setSelection({ kind: "preset", days: 84 });
    });

    expect(a.result.current.selection).toEqual({ kind: "preset", days: 1 });
    expect(a.result.current.tz).toBe("utc");
    expect(b.result.current.selection).toEqual({ kind: "preset", days: 84 });
    expect(b.result.current.tz).toBe("local");
    expect(JSON.parse(localStorage.getItem(keyA) as string)).toEqual({
      selection: { kind: "preset", days: 1 },
      tz: "utc",
    });
    expect(JSON.parse(localStorage.getItem(keyB) as string)).toEqual({
      selection: { kind: "preset", days: 84 },
      tz: "local",
    });
  });
});

describe("isRangeState", () => {
  it("accepts a valid {selection, tz} unit", () => {
    expect(
      isRangeState({ selection: { kind: "preset", days: 7 }, tz: "local" }),
    ).toBe(true);
    expect(
      isRangeState({
        selection: { kind: "custom", from: "a", to: "b" },
        tz: "utc",
      }),
    ).toBe(true);
  });

  it("rejects a missing or invalid tz", () => {
    expect(isRangeState({ selection: { kind: "preset", days: 7 } })).toBe(false);
    expect(
      isRangeState({ selection: { kind: "preset", days: 7 }, tz: "pst" }),
    ).toBe(false);
  });

  it("rejects a missing or invalid selection", () => {
    expect(isRangeState({ tz: "local" })).toBe(false);
    expect(isRangeState({ selection: { kind: "bogus" }, tz: "local" })).toBe(
      false,
    );
  });

  it("rejects null and non-objects", () => {
    expect(isRangeState(null)).toBe(false);
    expect(isRangeState(42)).toBe(false);
  });
});
