/**
 * Tests for app/(app)/profile/page.tsx — own profile page.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Authentication: `Role`, `Google`, and
 *     `Password` are read-only, driven by `role`, `has_google`, and
 *     `has_password` from `GET /auth/me`; the password section titles itself
 *     "Change password" when `has_password` is true and "Set a password" when it
 *     is false, and both write through the same `PATCH /auth/me` `password` field.
 *   - spec/feature/AUTH.md §Credential reset on link: a bind clears the row's
 *     password, so the not-set branch is the state a user lands in afterwards.
 *
 * Mocked: useMe, useUpdateProfile, toast — Vitest unit tier (no API).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import type { Me } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

const mockUpdateProfile = vi.fn();
vi.mock("@/lib/api/auth", () => ({
  useUpdateProfile: () => ({ mutateAsync: mockUpdateProfile, isPending: false }),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    error_code: string;
    trace_id: string;
    status: number;
    constructor(
      payload: { error_code: string; message: string; trace_id: string },
      status: number,
    ) {
      super(payload.message);
      this.name = "ApiError";
      this.error_code = payload.error_code;
      this.trace_id = payload.trace_id;
      this.status = status;
    }
  }
  return { ApiError, apiFetch: vi.fn() };
});

import ProfilePage from "./page";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMe(overrides: Partial<Me> = {}): Me {
  return {
    id: "u1",
    email: "alice@imazon.com",
    name: "Alice",
    role: "Editor",
    has_password: true,
    has_google: false,
    created_at: "2026-01-15T00:00:00Z",
    updated_at: "2026-01-15T00:00:00Z",
    ...overrides,
  };
}

function mockMe(overrides: Partial<Me> = {}) {
  mockUseMeFn.mockReturnValue({
    me: makeMe(overrides),
    isAdmin: false,
    isEditor: true,
    canWrite: true,
    isLoading: false,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockMe();
  mockUpdateProfile.mockResolvedValue(makeMe());
});

// ---------------------------------------------------------------------------
// 1. Read-only Password field
// ---------------------------------------------------------------------------
describe("ProfilePage — Password field (FRONTEND_BASIC.md §Authentication)", () => {
  it("shows 'Set' when has_password is true", async () => {
    render(<ProfilePage />);
    expect(await screen.findByDisplayValue("Set")).toBeTruthy();
  });

  it("shows 'Not set' when has_password is false", async () => {
    mockMe({ has_password: false, has_google: true });
    render(<ProfilePage />);
    expect(await screen.findByDisplayValue("Not set")).toBeTruthy();
  });

  it("renders the Password field alongside the Google field", async () => {
    mockMe({ has_google: true });
    render(<ProfilePage />);
    expect(await screen.findByText("Password")).toBeTruthy();
    expect(screen.getByText("Google")).toBeTruthy();
    expect(screen.getByDisplayValue("Linked")).toBeTruthy();
  });

  it("leaves the Password field read-only", async () => {
    render(<ProfilePage />);
    const input = (await screen.findByDisplayValue("Set")) as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2. Password section switches on has_password
// ---------------------------------------------------------------------------
describe("ProfilePage — password section (FRONTEND_BASIC.md §Authentication)", () => {
  it("titles the section 'Change password' with the keep-current hint when a password is set", async () => {
    render(<ProfilePage />);
    expect(await screen.findByText("Change password")).toBeTruthy();
    expect(screen.queryByText("Set a password")).toBeNull();
    expect(screen.getByText(/leave blank to keep your current password/i)).toBeTruthy();
  });

  it("titles the section 'Set a password' and explains the credential reset when none is set", async () => {
    mockMe({ has_password: false, has_google: true });
    render(<ProfilePage />);
    expect(await screen.findByText("Set a password")).toBeTruthy();
    expect(screen.queryByText("Change password")).toBeNull();
    // The hint has to name the cause, so a user whose password was cleared by a
    // Google bind can tell what happened and how to recover. The same bind
    // revoked their API tokens (AUTH.md §Credential reset on link), so the hint
    // points at that too.
    expect(screen.getByText(/signing in with google clears one that was set/i)).toBeTruthy();
    expect(screen.getByText(/re-mint any tokens you still need/i)).toBeTruthy();
  });

  it("writes the same PATCH /auth/me password field from the not-set branch", async () => {
    mockMe({ has_password: false, has_google: true });
    const user = userEvent.setup();
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText(/new password/i), "correcthorsebattery");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(mockUpdateProfile).toHaveBeenCalledWith({ password: "correcthorsebattery" }),
    );
  });

  it("writes the same PATCH /auth/me password field from the set branch", async () => {
    const user = userEvent.setup();
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText(/new password/i), "correcthorsebattery");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(mockUpdateProfile).toHaveBeenCalledWith({ password: "correcthorsebattery" }),
    );
  });
});
