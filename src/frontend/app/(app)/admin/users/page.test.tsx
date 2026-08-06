/**
 * Tests for app/(app)/admin/users/page.tsx — the ⋯ menu's "Unlink Google"
 * action and the per-user token drawer.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Authentication: the ⋯ menu carries
 *     "unlink Google" — `DELETE /admin/users/{id}/google` behind a ConfirmDialog
 *     that states the consequence (the user's sessions end and they sign in
 *     again), shown only for rows with `has_google` and disabled for rows
 *     without `has_password`, since the route refuses those with
 *     `409 GOOGLE_IS_ONLY_AUTH_METHOD`.
 *   - spec/feature/FRONTEND_BASIC.md §Authentication: "manage tokens", a drawer
 *     listing the user's `api_tokens` rows "with the same three-state Status
 *     column as `/profile/tokens`, a 'Show revoked' toggle driving the route's
 *     `include_revoked` param, and per-token revoke buttons on every unrevoked
 *     row".
 *   - spec/API.md §/admin/users/{id}/google: 204 on success; 409
 *     GOOGLE_IS_ONLY_AUTH_METHOD when the row has no password.
 *   - spec/API.md §Admin, `GET /admin/users/{id}/api-tokens`: "paginated with the
 *     standard `offset`/`limit`/`total_count` envelope; content key `tokens`…
 *     `?include_revoked=true` also returns rows with `revoked_at` set; default
 *     `false` (unrevoked rows only — expiry is not filtered)".
 *   - spec/feature/AUTH.md §Admin unbind: the unbind increments session_epoch,
 *     so sessions established under the released binding do not survive it.
 *
 * Mocked: useMe, the admin hooks, toast, timezone — Vitest unit tier (no API).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import React from "react";
import type { AdminApiTokenItem, AdminUser } from "@/lib/api/types";

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
const mockUseAdminUserTokens = vi.fn();
const mockRevokeUserToken = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useAdminUsers: () => mockUseAdminUsers(),
  useUpdateUserName: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateUserRole: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteUser: () => ({ mutateAsync: mockDeleteUser, isPending: false }),
  useUnlinkUserGoogle: () => ({ mutateAsync: mockUnlinkGoogle, isPending: false }),
  useAdminUserTokens: (userId: string, params?: { includeRevoked?: boolean }) =>
    mockUseAdminUserTokens(userId, params),
  useDeleteAdminUserToken: () => ({ mutateAsync: mockRevokeUserToken, isPending: false }),
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
    // The standard list envelope: offset / limit / total_count around the
    // content key. spec/API.md §Admin, `GET /admin/users`.
    data: { offset: 0, limit: 20, total_count: users.length, users },
    isLoading: false,
  });
  render(<AdminUsersPage />);
}

/**
 * The drawer's rows — one per status, so neither the Status column nor the
 * per-row revoke affordance can be satisfied by a single-state fixture. Stamps
 * are pushed far into the past / future so a row's status is decided here and
 * not by when the suite runs.
 */
const LONG_PAST = "2020-02-01T00:00:00Z";
const LONG_FUTURE = "2099-07-01T00:00:00Z";

const USER_TOKENS: AdminApiTokenItem[] = [
  {
    id: "t-active",
    name: "ci-jenkins",
    role_snapshot: "Editor",
    created_at: "2026-04-01T00:00:00Z",
    last_used_at: "2026-05-25T00:00:00Z",
    expires_at: LONG_FUTURE,
    revoked_at: null,
    user_id: "u-1",
    user_email: "alice@imazon.com",
  },
  {
    id: "t-expired",
    name: "etl-runner",
    role_snapshot: "Editor",
    created_at: "2025-11-02T00:00:00Z",
    last_used_at: "2026-01-08T00:00:00Z",
    expires_at: LONG_PAST,
    revoked_at: null,
    user_id: "u-1",
    user_email: "alice@imazon.com",
  },
  {
    id: "t-revoked",
    name: "laptop-cli",
    role_snapshot: "Editor",
    created_at: "2026-05-10T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: "2026-05-20T00:00:00Z",
    user_id: "u-1",
    user_email: "alice@imazon.com",
  },
];

/** More rows exist than this page carries, so the drawer's count line renders. */
const USER_TOKENS_TOTAL = 7;

/** Radix opens its dropdown on pointerdown, not click. */
function openRowMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: /more actions/i }));
}

/** Open the ⋯ menu's token drawer for the single rendered row. */
async function openTokenDrawer() {
  openRowMenu();
  fireEvent.click(await screen.findByText("Manage tokens"));
  return within(await screen.findByRole("dialog"));
}

/** The drawer row carrying `name`, as a scope for per-row assertions. */
function tokenRow(drawer: ReturnType<typeof within>, name: string): HTMLElement {
  const row = drawer.getByRole("cell", { name }).closest("tr");
  if (!row) throw new Error(`No drawer row found for "${name}"`);
  return row as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAdminUserTokens.mockReturnValue({
    data: { offset: 0, limit: 20, total_count: USER_TOKENS_TOTAL, tokens: USER_TOKENS },
    isLoading: false,
    error: null,
  });
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

// ---------------------------------------------------------------------------
// 3. Token drawer — Status column + Show revoked
// ---------------------------------------------------------------------------
describe("AdminUsersPage — token drawer (FRONTEND_BASIC.md §Authentication)", () => {
  it("reads the user's tokens with the route's default filter, revoked excluded", async () => {
    renderWithUsers([makeUser()]);
    await openTokenDrawer();

    // The drawer addresses the row it was opened from, and asks for the
    // default view — `include_revoked` off.
    expect(mockUseAdminUserTokens).toHaveBeenCalledWith(
      "u-1",
      expect.objectContaining({ includeRevoked: false }),
    );
    expect(mockUseAdminUserTokens).not.toHaveBeenCalledWith(
      "u-1",
      expect.objectContaining({ includeRevoked: true }),
    );
  });

  it("carries the same three-state Status column as /profile/tokens", async () => {
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    expect(drawer.getByRole("columnheader", { name: "Status" })).toBeTruthy();
    // One row per state, so a column that hardcoded a single label would fail.
    expect(within(tokenRow(drawer, "ci-jenkins")).getByText("active")).toBeTruthy();
    expect(within(tokenRow(drawer, "etl-runner")).getByText("expired")).toBeTruthy();
    expect(within(tokenRow(drawer, "laptop-cli")).getByText("revoked")).toBeTruthy();
  });

  it("offers revoke on every unrevoked row and none on the revoked one", async () => {
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    expect(
      within(tokenRow(drawer, "ci-jenkins")).getByRole("button", { name: "Revoke" }),
    ).toBeTruthy();
    // Expiry is a clock, revocation is a decision — an expired token is still
    // revocable.
    expect(
      within(tokenRow(drawer, "etl-runner")).getByRole("button", { name: "Revoke" }),
    ).toBeTruthy();
    // A revoked token grants nothing, so there is nothing left to withdraw.
    expect(
      within(tokenRow(drawer, "laptop-cli")).queryByRole("button", { name: "Revoke" }),
    ).toBeNull();
  });

  it("turns Show revoked into the route's include_revoked param", async () => {
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    const toggle = drawer.getByLabelText("Show revoked");
    expect(toggle).toHaveAttribute("data-state", "unchecked");

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(mockUseAdminUserTokens).toHaveBeenCalledWith(
        "u-1",
        expect.objectContaining({ includeRevoked: true }),
      ),
    );
  });

  it("says how many of the user's tokens this page holds", async () => {
    // spec/API.md §Admin — the per-user read is "paginated with the standard
    // `offset`/`limit`/`total_count` envelope", so a drawer showing three rows
    // out of seven must not read as "this user has three tokens". The wording
    // is the page's own; only the two numbers are held to the envelope.
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    expect(
      drawer.getByText(new RegExp(String.raw`\b${USER_TOKENS.length}\s*of\s*${USER_TOKENS_TOTAL}\b`)),
    ).toBeTruthy();
  });

  it("distinguishes an empty default view from an empty inventory", async () => {
    // spec/feature/AUTH.md §Revoked-token visibility — withdrawn rows stay out
    // of the default view behind an explicit opt-in, so "no rows" with the
    // toggle off does not mean the user never held a token. The two states must
    // therefore read differently, and the default one must say which scope it
    // is empty of; the exact sentences are the page's to choose.
    mockUseAdminUserTokens.mockReturnValue({
      data: { offset: 0, limit: 20, total_count: 0, tokens: [] },
      isLoading: false,
      error: null,
    });
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    const defaultEmpty = drawer.getByText(/^no\b.*tokens/i).textContent ?? "";
    expect(defaultEmpty).toMatch(/active/i);

    fireEvent.click(drawer.getByLabelText("Show revoked"));

    await waitFor(() => {
      const allEmpty = drawer.getByText(/^no\b.*tokens/i).textContent ?? "";
      expect(allEmpty).not.toBe(defaultEmpty);
      // With revoked rows included there is no narrower scope left to name.
      expect(allEmpty).not.toMatch(/active/i);
    });
  });

  it("revokes a token through the per-user admin route", async () => {
    mockRevokeUserToken.mockResolvedValue(undefined);
    renderWithUsers([makeUser()]);
    const drawer = await openTokenDrawer();

    fireEvent.click(within(tokenRow(drawer, "ci-jenkins")).getByRole("button", { name: "Revoke" }));

    // The confirm is a second dialog; scope to the one carrying the confirm copy.
    const confirm = await screen.findByText(/permanently revoke/i);
    const confirmDialog = within(confirm.closest('[role="dialog"]') as HTMLElement);
    expect(mockRevokeUserToken).not.toHaveBeenCalled();

    fireEvent.click(confirmDialog.getByRole("button", { name: "Revoke" }));

    await waitFor(() =>
      expect(mockRevokeUserToken).toHaveBeenCalledWith({ userId: "u-1", tokenId: "t-active" }),
    );
  });
});
