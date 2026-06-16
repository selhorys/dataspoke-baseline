/**
 * Tests for lib/preferences/timezone.ts — the global display-timezone store.
 *
 * Behaviour under test:
 *   - default tz is "local"
 *   - setTz updates the store and persists to localStorage under the
 *     "dataspoke:timezone" key
 *   - rehydrate() reads a previously persisted value back into the store
 *
 * Persistence uses zustand's persist middleware with skipHydration:true, so the
 * store does NOT read storage until rehydrate() is called (the SSR-safe path).
 *
 * Spec trace:
 *   - spec/feature/FRONTEND_BASIC.md §/settings: "timezone (Local or UTC,
 *     **default Local**) … persisted in localStorage only. The timezone
 *     preference is display-only — it governs how all dates and times are
 *     rendered across the app."
 *
 * Isolation note: the store uses zustand persist (skipHydration:true). It does
 * not auto-read storage, so resetting via setState({tz:"local"}) + clearing
 * localStorage in beforeEach/afterEach fully isolates each test — no rehydrate
 * fires unless a test calls it explicitly.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useTimezoneStore, useDisplayTz } from "./timezone";

beforeEach(() => {
  localStorage.clear();
  act(() => {
    useTimezoneStore.setState({ tz: "local" });
  });
});

afterEach(() => {
  localStorage.clear();
});

describe("useTimezoneStore", () => {
  it("defaults to local", () => {
    expect(useTimezoneStore.getState().tz).toBe("local");
  });

  it("setTz updates state and persists to localStorage", () => {
    act(() => {
      useTimezoneStore.getState().setTz("utc");
    });

    expect(useTimezoneStore.getState().tz).toBe("utc");

    const raw = localStorage.getItem("dataspoke:timezone");
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string).state.tz).toBe("utc");
  });

  it("rehydrate() restores a previously persisted timezone", async () => {
    localStorage.setItem(
      "dataspoke:timezone",
      JSON.stringify({ state: { tz: "utc" }, version: 0 }),
    );

    await act(async () => {
      await useTimezoneStore.persist.rehydrate();
    });

    expect(useTimezoneStore.getState().tz).toBe("utc");
  });

  it("rehydrate() picks up a pre-seeded localStorage value into the store", async () => {
    // Distinct from the test above only in intent: confirm a value already
    // present in storage before any store interaction is adopted on rehydrate.
    localStorage.setItem(
      "dataspoke:timezone",
      JSON.stringify({ state: { tz: "utc" }, version: 0 }),
    );
    // Default until rehydrate runs (skipHydration).
    expect(useTimezoneStore.getState().tz).toBe("local");

    await act(async () => {
      await useTimezoneStore.persist.rehydrate();
    });

    expect(useTimezoneStore.getState().tz).toBe("utc");
  });
});

describe("useDisplayTz", () => {
  it("returns the current display timezone (default local)", () => {
    const { result } = renderHook(() => useDisplayTz());
    expect(result.current).toBe("local");
  });

  it("reflects setTz reactively", () => {
    const { result } = renderHook(() => useDisplayTz());
    act(() => {
      useTimezoneStore.getState().setTz("utc");
    });
    expect(result.current).toBe("utc");
  });
});
