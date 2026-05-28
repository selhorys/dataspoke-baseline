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

// getRuntimeConfig — controllable per test via mockGetRuntimeConfig
const mockGetRuntimeConfig = vi.fn();
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => mockGetRuntimeConfig(),
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
  // Default: no infra URLs configured
  mockGetRuntimeConfig.mockReturnValue({ datahubUrl: "", langfuseUrl: "", airflowUrl: "", apiBaseUrl: "" });
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

describe("AppShell — infra icon links", () => {
  beforeEach(() => {
    mockUseMe.mockReturnValue({
      me: makeMe("Reader"),
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });
  });

  it("renders all four infra links when all URLs are configured", () => {
    mockGetRuntimeConfig.mockReturnValue({
      datahubUrl: "http://datahub.example.com",
      langfuseUrl: "http://langfuse.example.com",
      airflowUrl: "http://airflow.example.com",
      apiBaseUrl: "http://api.example.com",
    });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    const datahubLink = screen.getByRole("link", { name: /open datahub/i });
    expect(datahubLink).toBeTruthy();
    expect(datahubLink.getAttribute("href")).toBe("http://datahub.example.com");
    expect(datahubLink.getAttribute("target")).toBe("_blank");

    const langfuseLink = screen.getByRole("link", { name: /open langfuse/i });
    expect(langfuseLink).toBeTruthy();
    expect(langfuseLink.getAttribute("href")).toBe("http://langfuse.example.com");
    expect(langfuseLink.getAttribute("target")).toBe("_blank");

    const airflowLink = screen.getByRole("link", { name: /open airflow/i });
    expect(airflowLink).toBeTruthy();
    expect(airflowLink.getAttribute("href")).toBe("http://airflow.example.com");
    expect(airflowLink.getAttribute("target")).toBe("_blank");

    const apiDocsLink = screen.getByRole("link", { name: /open api docs/i });
    expect(apiDocsLink).toBeTruthy();
    expect(apiDocsLink.getAttribute("href")).toBe("http://api.example.com/redoc");
    expect(apiDocsLink.getAttribute("target")).toBe("_blank");
  });

  it("omits a link when its URL is empty", () => {
    mockGetRuntimeConfig.mockReturnValue({
      datahubUrl: "http://datahub.example.com",
      langfuseUrl: "",
      airflowUrl: "",
      apiBaseUrl: "",
    });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    expect(screen.getByRole("link", { name: /open datahub/i })).toBeTruthy();
    expect(screen.queryByRole("link", { name: /open langfuse/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /open airflow/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /open api docs/i })).toBeNull();
  });

  it("renders no infra links when all URLs are empty", () => {
    mockGetRuntimeConfig.mockReturnValue({ datahubUrl: "", langfuseUrl: "", airflowUrl: "", apiBaseUrl: "" });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    expect(screen.queryByRole("link", { name: /open datahub/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /open langfuse/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /open airflow/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /open api docs/i })).toBeNull();
  });
});
