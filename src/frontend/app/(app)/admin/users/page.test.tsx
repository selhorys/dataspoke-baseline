/**
 * Tests for app/(app)/admin/users/page.tsx — the ⋯ menu's "Unlink Google" action.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Authentication: the ⋯ menu carries
 *     "unlink Google" — `DELETE /admin/users/{id}/google` behind a ConfirmDialog
 *     that states the consequence (the user's sessions end and they sign in
 *     again), shown only for rows with `has_google` and disabled for rows
 *     without `has_password`, since the route refuses those with
 *     `409 GOOGLE_IS_ONLY_AUTH_METHOD`.
 *   - spec/API.md §/admin/users/{id}/google: 204 on success; 409
 *     GOOGLE_IS_ONLY_AUTH_METHOD when the row has no password.
 *   - spec/feature/AUTH.md §Admin unbind: the unbind increments session_epoch,
 *     so sessions established under the released binding do not survive it.
 *
 * Mocked: useMe, the admin hooks, toast, timezone — Vitest unit tier (no API).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import React from "react";
import type { AdminUser } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Browser API stubs — jsdom lacks ResizeObserver / pointer capture, both of
// which the Radix Select in the role column and the ⋯ dropdown rely on.
// ---------------------------------------------------------------------------
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

const mockUseAdminUsers = vi.fn();
const mockUnlinkGoogle = vi.fn();
const mockDeleteUser = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useAdminUsers: () => mockUseAdminUsers(),
  useUpdateUserName: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateUserRole: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteUser: () => ({ mutateAsync: mockDeleteUser, isPending: false }),
  useUnlinkUserGoogle: () => ({ mutateAsync: mockUnlinkGoogle, isPending: false }),
  useAdminUserTokens: () => ({ data: { tokens: [], total: 0 }, isLoading: false }),
  useDeleteAdminUserToken: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock("@/lib/preferences/timezone", () => ({
  useDisplayTz: () => "UTC",
}));

// ApiError — mirror the real constructor signature (payload, status)
vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    error_code: string;
    trace_id: string;
    status: number;
    constructor(
      payload: { error_code: string; message: string; trace_id: string; resp_time: string },
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

import AdminUsersPage from "./page";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeUser(overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: "u-1",
    email: "alice@imazon.com",
    name: "Alice",
    role: "Editor",
    has_password: true,
    has_google: true,
    created_at: "2026-01-15T00:00:00Z",
    updated_at: "2026-01-15T00:00:00Z",
    ...overrides,
  };
}

function renderWithUsers(users: AdminUser[]) {
  mockUseAdminUsers.mockReturnValue({
    data: { users, total: users.length },
    isLoading: false,
  });
  render(<AdminUsersPage />);
}

/** Radix opens its dropdown on pointerdown, not click. */
function openRowMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: /more actions/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMeFn.mockReturnValue({
    me: {
      id: "admin-1",
      email: "admin@imazon.com",
      name: "Admin",
      role: "Admin" as const,
      has_password: true,
      has_google: false,
      created_at: "",
      updated_at: "",
    },
    isAdmin: true,
    isEditor: false,
    canWrite: true,
    isLoading: false,
  });
  mockUnlinkGoogle.mockResolvedValue(undefined);
});

// ---------------------------------------------------------------------------
// 1. Visibility + enablement
// ---------------------------------------------------------------------------
describe("AdminUsersPage — Unlink Google visibility (FRONTEND_BASIC.md §Authentication)", () => {
  it("offers the action for a row that has a Google binding and a password", async () => {
    renderWithUsers([makeUser()]);
    openRowMenu();
    const item = await screen.findByText("Unlink Google");
    expect(item.getAttribute("aria-disabled")).not.toBe("true");
  });

  it("omits the action for a row with no Google binding", async () => {
    renderWithUsers([makeUser({ has_google: false })]);
    openRowMenu();
    // The menu itself opened — the other items prove it.
    expect(await screen.findByText("Manage tokens")).toBeTruthy();
    expect(screen.queryByText("Unlink Google")).toBeNull();
  });

  it("disables the action for a row with no password, which the route refuses 409", async () => {
    renderWithUsers([makeUser({ has_password: false })]);
    openRowMenu();
    const item = await screen.findByText("Unlink Google");
    expect(item.getAttribute("aria-disabled")).toBe("true");

    fireEvent.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mockUnlinkGoogle).not.toHaveBeenCalled();
  });

  it("explains on the disabled action why the route would refuse it", async () => {
    renderWithUsers([makeUser({ has_password: false })]);
    openRowMenu();
    const item = await screen.findByText("Unlink Google");
    expect(item.getAttribute("title")).toMatch(/no password/i);
    expect(item.getAttribute("title")).toMatch(/password reset/i);
  });
});

// ---------------------------------------------------------------------------
// 2. Confirm dialog + call
// ---------------------------------------------------------------------------
describe("AdminUsersPage — Unlink Google confirmation (AUTH.md §Admin unbind)", () => {
  it("gates the action behind a ConfirmDialog that states the sign-out consequence", async () => {
    renderWithUsers([makeUser()]);
    openRowMenu();
    fireEvent.click(await screen.findByText("Unlink Google"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Unlink Google" })).toBeTruthy();
    expect(within(dialog).getByText(/alice@imazon\.com/)).toBeTruthy();
    expect(within(dialog).getByText(/signed out of every session/i)).toBeTruthy();
    // Opening the dialog alone must not call the route.
    expect(mockUnlinkGoogle).not.toHaveBeenCalled();
  });

  it("calls DELETE /admin/users/{id}/google on confirm and reports success", async () => {
    renderWithUsers([makeUser()]);
    openRowMenu();
    fireEvent.click(await screen.findByText("Unlink Google"));
    fireEvent.click(await screen.findByRole("button", { name: /^unlink$/i }));

    await waitFor(() => expect(mockUnlinkGoogle).toHaveBeenCalledWith({ id: "u-1" }));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: expect.stringContaining("alice@imazon.com") }),
      ),
    );
  });

  it("surfaces the API message when the route refuses the unbind", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockUnlinkGoogle.mockRejectedValue(
      new ApiError(
        {
          error_code: "GOOGLE_IS_ONLY_AUTH_METHOD",
          message: "Releasing the binding would leave the user with no authentication method.",
          trace_id: "t-1",
          resp_time: "2026-07-24T00:00:00Z",
        },
        409,
      ),
    );
    renderWithUsers([makeUser()]);
    openRowMenu();
    fireEvent.click(await screen.findByText("Unlink Google"));
    fireEvent.click(await screen.findByRole("button", { name: /^unlink$/i }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description:
            "Releasing the binding would leave the user with no authentication method.",
        }),
      ),
    );
  });

  it("stays silent on a 401, which the auth client already resolves by redirecting", async () => {
    // A superseded session epoch (AUTH.md §Session epoch) reaches this handler as
    // an ordinary mid-session 401; the client has cleared the session and the
    // guard is redirecting, so a failure toast would land over /login.
    const { ApiError } = await import("@/lib/api/client");
    mockUnlinkGoogle.mockRejectedValue(
      new ApiError(
        {
          error_code: "UNAUTHORIZED",
          message: "Not authenticated",
          trace_id: "t-2",
          resp_time: "2026-07-24T00:00:00Z",
        },
        401,
      ),
    );
    renderWithUsers([makeUser()]);
    openRowMenu();
    fireEvent.click(await screen.findByText("Unlink Google"));
    fireEvent.click(await screen.findByRole("button", { name: /^unlink$/i }));

    await waitFor(() => expect(mockUnlinkGoogle).toHaveBeenCalled());
    expect(mockToast).not.toHaveBeenCalled();
  });
});
