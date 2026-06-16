/**
 * Tests for lib/hooks/use-range-selection.ts — localStorage-backed
 * RangeSelection.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "The selection persists across visits in browser localStorage under a
 *     stable key per logical panel — each panel persists independently … so
 *     revisiting a panel restores the last-used selection." Default is the
 *     2-week preset.
 *   - lib/range.ts: persisted value is the RangeSelection (intent), guarded by
 *     isRangeSelection on read; corrupt/unavailable storage falls back to the
 *     default selection. Display timezone is governed globally
 *     (lib/preferences/timezone.ts), not per panel.
 *
 * The initial render uses the SSR-safe default; a post-mount useEffect hydrates
 * from localStorage, so hydration assertions wait for that effect.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { usePersistedRangeState } from "./use-range-selection";
import { defaultSelection, type RangeSelection } from "@/lib/range";

const KEY = "range:test:panel";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("usePersistedRangeState — initial value", () => {
  it("is the documented default (2-week preset) when storage is empty", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));
    expect(result.current.selection).toEqual(defaultSelection());
    expect(result.current.selection).toEqual({ kind: "preset", days: 14 });
  });
});

describe("usePersistedRangeState — hydration from storage", () => {
  it("hydrates to a valid stored preset selection after mount", async () => {
    const stored: RangeSelection = { kind: "preset", days: 7 };
    localStorage.setItem(KEY, JSON.stringify(stored));

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await waitFor(() => {
      expect(result.current.selection).toEqual(stored);
    });
  });

  it("hydrates to a valid stored custom selection after mount", async () => {
    const stored: RangeSelection = {
      kind: "custom",
      from: "2024-03-01T00:00:00.000Z",
      to: "2024-03-05T23:59:59.999Z",
    };
    localStorage.setItem(KEY, JSON.stringify(stored));

    const { result } = renderHook(() => usePersistedRangeState(KEY));

    await waitFor(() => {
      expect(result.current.selection).toEqual(stored);
    });
  });
});

describe("usePersistedRangeState — setter persistence", () => {
  it("setSelection updates state and writes the selection to localStorage", () => {
    const { result } = renderHook(() => usePersistedRangeState(KEY));

    const next: RangeSelection = { kind: "preset", days: 28 };
    act(() => {
      result.current.setSelection(next);
    });

    expect(result.current.selection).toEqual(next);
    const raw = localStorage.getItem(KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string)).toEqual(next);
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
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(next);
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
  });

  it("ignores well-formed JSON that fails the shape guard", async () => {
    localStorage.setItem(KEY, JSON.stringify({ kind: "bogus" }));

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
    });
    act(() => {
      b.result.current.setSelection({ kind: "preset", days: 84 });
    });

    expect(a.result.current.selection).toEqual({ kind: "preset", days: 1 });
    expect(b.result.current.selection).toEqual({ kind: "preset", days: 84 });
    expect(JSON.parse(localStorage.getItem(keyA) as string)).toEqual({
      kind: "preset",
      days: 1,
    });
    expect(JSON.parse(localStorage.getItem(keyB) as string)).toEqual({
      kind: "preset",
      days: 84,
    });
  });
});
