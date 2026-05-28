/**
 * Tests for components/app-shell.tsx — role-gated navigation links.
 *
 * Spec traces:
 *   - spec/API.md §Auth: Admin role has user-management access; Editor/Reader do not
 *   - spec/feature/FRONTEND_BASIC.md §Routing: the UI hides the admin-menu entry when
 *     the role is not Admin; write actions rendered only when role ∈ {Editor, Admin}
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { AppShell } from "./app-shell";
import type { Me } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Mock heavy dependencies that AppShell pulls in
// ---------------------------------------------------------------------------
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/ingestion",
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

vi.mock("@/lib/api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue(undefined),
}));

// Mock useMe so we can control isAdmin without a real query
const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMe(),
}));

// Provide a minimal stub for useAuthStore
vi.mock("@/lib/auth/store", () => ({
  useAuthStore: (selector: (s: { clear: () => void }) => unknown) =>
    selector({ clear: vi.fn() }),
}));

// ThemeToggle uses next-themes internals — stub it out
vi.mock("@/components/theme-toggle", () => ({
  ThemeToggle: () => React.createElement("div", { "data-testid": "theme-toggle" }),
}));

// NotificationCenter uses TanStack Query — stub it out in shell unit tests
vi.mock("@/components/notification-center", () => ({
  NotificationCenter: () => React.createElement("div", { "data-testid": "notification-center" }),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function makeMe(role: Me["role"]): Me {
  return {
    id: "u1",
    email: "user@example.com",
    name: "Test User",
    role,
    has_google: false,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
  };
}

beforeEach(() => {
  mockUseMe.mockReset();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe("AppShell — Admin Users link visibility", () => {
  it("shows the Users sidebar link when the user is Admin", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // The sidebar must contain a link to /admin/users
    const usersLink = screen.getByRole("link", { name: /users/i });
    expect(usersLink).toBeTruthy();
  });

  it("hides the Users sidebar link for Editor role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Editor"), isAdmin: false, isEditor: true, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // No link to /admin/users
    const usersLinks = screen.queryAllByRole("link", { name: /^users$/i });
    const adminLink = usersLinks.find((el) => el.getAttribute("href") === "/admin/users");
    expect(adminLink).toBeUndefined();
  });

  it("hides the Users sidebar link for Reader role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Reader"), isAdmin: false, isEditor: false, canWrite: false, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    const usersLinks = screen.queryAllByRole("link", { name: /^users$/i });
    const adminLink = usersLinks.find((el) => el.getAttribute("href") === "/admin/users");
    expect(adminLink).toBeUndefined();
  });

  it("renders main nav links regardless of role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Reader"), isAdmin: false, isEditor: false, canWrite: false, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // Ingestion and Validation links must always be visible
    expect(screen.getByRole("link", { name: /ingestion/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /validation/i })).toBeTruthy();
  });
});
