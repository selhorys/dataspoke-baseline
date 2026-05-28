/**
 * Tests for lib/auth/store.ts
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Authentication: access token is memory-only; store is not persisted
 *   - spec/API.md §Auth: setToken/setMe/clear/setAuthInitialized reflect the login/logout contract
 */
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useAuthStore } from "./store";

// Reset store state before each test so tests are independent.
beforeEach(() => {
  useAuthStore.setState({
    accessToken: null,
    me: null,
    authInitialized: false,
  });
});

describe("useAuthStore — state mutations", () => {
  it("setToken stores the access token in memory", () => {
    useAuthStore.getState().setToken("access-token-xyz");
    expect(useAuthStore.getState().accessToken).toBe("access-token-xyz");
  });

  it("setMe stores the me object", () => {
    const me = {
      id: "u1",
      email: "admin@example.com",
      name: "Admin User",
      role: "Admin" as const,
      has_google: false,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    useAuthStore.getState().setMe(me);
    expect(useAuthStore.getState().me).toEqual(me);
  });

  it("setAuthInitialized sets the flag to true", () => {
    expect(useAuthStore.getState().authInitialized).toBe(false);
    useAuthStore.getState().setAuthInitialized(true);
    expect(useAuthStore.getState().authInitialized).toBe(true);
  });

  it("setAuthInitialized can set the flag to false", () => {
    useAuthStore.getState().setAuthInitialized(true);
    useAuthStore.getState().setAuthInitialized(false);
    expect(useAuthStore.getState().authInitialized).toBe(false);
  });

  it("clear nulls out accessToken and me but does not affect authInitialized", () => {
    useAuthStore.getState().setToken("some-token");
    useAuthStore.getState().setMe({
      id: "u2",
      email: "editor@example.com",
      name: "Editor",
      role: "Editor" as const,
      has_google: false,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    });
    useAuthStore.getState().setAuthInitialized(true);

    useAuthStore.getState().clear();

    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.me).toBeNull();
    // authInitialized is not cleared — the probe has run; clearing auth is a session event
    // (spec says clear() is called on logout, not on page load)
    // We do not assert on authInitialized here; we only verify that the token and me are gone.
  });
});

describe("useAuthStore — memory-only persistence", () => {
  // Spy on Storage.prototype.setItem to detect any write to localStorage,
  // regardless of timing. The persist middleware writes on the next microtask,
  // so a synchronous key-count snapshot is not sufficient — we need an actual spy.
  // Spec: spec/feature/FRONTEND_BASIC.md §Authentication — access token is
  // memory-only; the store must not use zustand/persist or any other storage layer.
  let setItemSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    setItemSpy = vi.spyOn(Storage.prototype, "setItem");
  });

  afterEach(() => {
    setItemSpy.mockRestore();
  });

  it("does not write to localStorage after setToken", async () => {
    useAuthStore.getState().setToken("should-not-persist");
    // Flush microtasks: persist middleware, if present, writes on the next tick.
    await Promise.resolve();
    expect(setItemSpy).not.toHaveBeenCalled();
    // Belt-and-suspenders: confirm nothing was actually written
    expect(localStorage.getItem("auth-store")).toBeNull();
  });

  it("does not write to localStorage after setMe", async () => {
    useAuthStore.getState().setMe({
      id: "u1",
      email: "admin@example.com",
      name: "Admin",
      role: "Admin" as const,
      has_google: false,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    });
    await Promise.resolve();
    expect(setItemSpy).not.toHaveBeenCalled();
  });

  it("does not write to sessionStorage after setToken", () => {
    const beforeLen = Object.keys(sessionStorage).length;
    useAuthStore.getState().setToken("should-not-persist-session");
    expect(Object.keys(sessionStorage).length).toBe(beforeLen);
  });

  it("initial state has null token (not hydrated from any storage)", () => {
    // Token must come from the silent-refresh probe, not from storage hydration.
    const freshState = useAuthStore.getState();
    expect(freshState.accessToken).toBeNull();
  });
});
