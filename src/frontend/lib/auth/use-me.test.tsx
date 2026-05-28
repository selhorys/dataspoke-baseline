/**
 * Tests for lib/auth/use-me.ts — role helper properties.
 *
 * Spec traces:
 *   - spec/API.md §Auth: UserRole is Admin | Editor | Reader (case-sensitive)
 *   - spec/feature/FRONTEND_BASIC.md: isAdmin/isEditor/canWrite derive from me.role,
 *     not from the JWT directly
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { useMe } from "./use-me";
import { useAuthStore } from "./store";
import type { Me } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMe(role: Me["role"]): Me {
  return {
    id: "test-user",
    email: "test@example.com",
    name: "Test User",
    role,
    has_google: false,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
  };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

// Reset store before each test
beforeEach(() => {
  useAuthStore.setState({ accessToken: null, me: null, authInitialized: false });
});

// ---------------------------------------------------------------------------
// Role derivation — driven from a role→expectation table
// ---------------------------------------------------------------------------
describe("useMe — role helpers derive from me.role", () => {
  const cases: Array<{
    role: Me["role"];
    isAdmin: boolean;
    isEditor: boolean;
    canWrite: boolean;
  }> = [
    { role: "Admin",  isAdmin: true,  isEditor: false, canWrite: true  },
    { role: "Editor", isAdmin: false, isEditor: true,  canWrite: true  },
    { role: "Reader", isAdmin: false, isEditor: false, canWrite: false },
  ];

  for (const { role, isAdmin, isEditor, canWrite } of cases) {
    it(`role=${role}: isAdmin=${isAdmin}, isEditor=${isEditor}, canWrite=${canWrite}`, () => {
      // Seed the store with the me object — this avoids a real network call
      act(() => {
        useAuthStore.setState({ me: makeMe(role), accessToken: "tok" });
      });

      const { result } = renderHook(() => useMe(), { wrapper });

      expect(result.current.isAdmin).toBe(isAdmin);
      expect(result.current.isEditor).toBe(isEditor);
      expect(result.current.canWrite).toBe(canWrite);
    });
  }

  it("default-denies all privileges for an unknown/unlisted role value", () => {
    // Spec: spec/API.md §Auth — UserRole is Admin | Editor | Reader (case-sensitive).
    // Any out-of-contract value must map to zero privileges: isAdmin=false,
    // isEditor=false, canWrite=false.
    act(() => {
      useAuthStore.setState({
        me: makeMe("SuperUser" as Me["role"]),
        accessToken: "tok",
      });
    });

    const { result } = renderHook(() => useMe(), { wrapper });
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isEditor).toBe(false);
    expect(result.current.canWrite).toBe(false);
  });

  it("returns me from the store when present (does not solely rely on query.data)", () => {
    const meObj = makeMe("Admin");
    act(() => {
      useAuthStore.setState({ me: meObj, accessToken: "tok" });
    });

    const { result } = renderHook(() => useMe(), { wrapper });
    expect(result.current.me).toEqual(meObj);
  });

  it("returns null me when store has no me and no token (query disabled)", () => {
    // No accessToken means the query is disabled; me should be null.
    act(() => {
      useAuthStore.setState({ me: null, accessToken: null });
    });

    const { result } = renderHook(() => useMe(), { wrapper });
    expect(result.current.me).toBeNull();
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.canWrite).toBe(false);
  });
});
