/**
 * Tests for components/app-shell.tsx — role-gated navigation links.
 *
 * Spec traces:
 *   - spec/API.md §Auth: Admin role has user-management access; Editor/Reader do not
 *   - spec/feature/FRONTEND_BASIC.md §Shell: Admin section (Users + Configurations links)
 *     renders ONLY when isAdmin is true; placed ABOVE the Account section which always
 *     renders for everyone; adminNav = [{label:"Users",href:"/admin/users"},
 *     {label:"Configurations",href:"/admin/conf"}]
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
  usePathname: () => "/validation",
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
describe("AppShell — Admin section visibility (FRONTEND_BASIC.md §Shell)", () => {
  it("shows the Admin section label when the user is Admin", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // The sidebar must contain the "Admin" section label (uppercase per spec)
    const adminLabel = screen.getByText(/^admin$/i);
    expect(adminLabel).toBeTruthy();
  });

  it("shows the Users link at /admin/users in the Admin section when isAdmin", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // adminNav[0]: {label:"Users", href:"/admin/users"}
    const usersLinks = screen.getAllByRole("link", { name: /^users$/i });
    const adminLink = usersLinks.find((el) => el.getAttribute("href") === "/admin/users");
    expect(adminLink).toBeTruthy();
  });

  it("shows the Configurations link at /admin/conf in the Admin section when isAdmin", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell: adminNav includes Configurations → /admin/conf
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // adminNav[1]: {label:"Configurations", href:"/admin/conf"}
    const confLink = screen.getByRole("link", { name: /configurations/i });
    expect(confLink).toBeTruthy();
    expect(confLink.getAttribute("href")).toBe("/admin/conf");
  });

  it("places Admin section ABOVE Account section in the DOM when isAdmin", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell: Admin section is rendered above Account section
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    const adminLabel = screen.getByText(/^admin$/i);
    const accountLabel = screen.getByText(/^account$/i);

    // compareDocumentPosition bit 0x04 = DOCUMENT_POSITION_FOLLOWING
    // adminLabel.compareDocumentPosition(accountLabel) & 0x04 being truthy means
    // accountLabel comes AFTER adminLabel (i.e., Admin is above Account in DOM)
    const position = adminLabel.compareDocumentPosition(accountLabel);
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("hides the Admin section label for Editor role", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell: Admin section renders only when isAdmin
    mockUseMe.mockReturnValue({ me: makeMe("Editor"), isAdmin: false, isEditor: true, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // The "Admin" section label must not appear
    expect(screen.queryByText(/^admin$/i)).toBeNull();
  });

  it("hides the Configurations link for Editor role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Editor"), isAdmin: false, isEditor: true, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    const confLinks = screen.queryAllByRole("link", { name: /configurations/i });
    const adminConfLink = confLinks.find((el) => el.getAttribute("href") === "/admin/conf");
    expect(adminConfLink).toBeUndefined();
  });

  it("hides the Admin section and Users/Configurations links for Reader role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Reader"), isAdmin: false, isEditor: false, canWrite: false, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    expect(screen.queryByText(/^admin$/i)).toBeNull();

    const usersLinks = screen.queryAllByRole("link", { name: /^users$/i });
    const adminUsersLink = usersLinks.find((el) => el.getAttribute("href") === "/admin/users");
    expect(adminUsersLink).toBeUndefined();

    const confLinks = screen.queryAllByRole("link", { name: /configurations/i });
    const adminConfLink = confLinks.find((el) => el.getAttribute("href") === "/admin/conf");
    expect(adminConfLink).toBeUndefined();
  });

  it("always renders the Account section regardless of role (Admin)", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell: Account section renders for everyone
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    expect(screen.getByText(/^account$/i)).toBeTruthy();
  });

  it("always renders the Account section for Reader role", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Reader"), isAdmin: false, isEditor: false, canWrite: false, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    expect(screen.getByText(/^account$/i)).toBeTruthy();
  });
});

describe("AppShell — Admin Users link visibility (legacy, preserved)", () => {
  it("shows the Users sidebar link when the user is Admin", () => {
    mockUseMe.mockReturnValue({ me: makeMe("Admin"), isAdmin: true, isEditor: false, canWrite: true, isLoading: false });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    // The sidebar must contain a link to /admin/users
    const usersLinks = screen.getAllByRole("link", { name: /^users$/i });
    const adminLink = usersLinks.find((el) => el.getAttribute("href") === "/admin/users");
    expect(adminLink).toBeTruthy();
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

    // Validation and MetaGen links must always be visible
    expect(screen.getByRole("link", { name: /validation/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /metagen/i })).toBeTruthy();
  });
});

describe("AppShell — Ingestion nav entry (FRONTEND_BASIC.md §Shell)", () => {
  it.each([["Admin"], ["Editor"], ["Reader"]] as const)(
    "renders the Ingestion link at /ingestion for %s role",
    (role) => {
      mockUseMe.mockReturnValue({
        me: makeMe(role),
        isAdmin: role === "Admin",
        isEditor: role === "Editor",
        canWrite: role !== "Reader",
        isLoading: false,
      });

      render(
        <AppShell>
          <div />
        </AppShell>,
      );

      const link = screen.getByRole("link", { name: /ingestion/i });
      expect(link).toBeTruthy();
      expect(link.getAttribute("href")).toBe("/ingestion");
    },
  );

  it("places Ingestion between the Governance group and Validation in the DOM", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell fixes the entries, grouping, and order:
    // Governance ▾ · Ingestion · Validation · OntoGen ▾ · MetaGen.
    // How the §Shell grouping is realized in this implementation: each ▾ group renders
    // as a disclosure <button> and a group stays collapsed unless its active route is
    // open. Under the /validation pathname mock the Governance group is collapsed, so its
    // leading anchor in the DOM is the group toggle <button>, not its child links.
    mockUseMe.mockReturnValue({
      me: makeMe("Admin"),
      isAdmin: true,
      isEditor: false,
      canWrite: true,
      isLoading: false,
    });

    render(
      <AppShell>
        <div />
      </AppShell>,
    );

    const governanceGroup = screen.getByRole("button", { name: /governance/i });
    const ingestion = screen.getByRole("link", { name: /ingestion/i });
    const validation = screen.getByRole("link", { name: /validation/i });

    // DOCUMENT_POSITION_FOLLOWING (0x04): the argument node comes AFTER `this`.
    expect(
      governanceGroup.compareDocumentPosition(ingestion) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      ingestion.compareDocumentPosition(validation) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
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
    expect(datahubLink.getAttribute("href")).toBe("http://datahub.example.com/login");
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
