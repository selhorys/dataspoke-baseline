/**
 * Tests for components/app-shell.tsx — role-gated navigation links.
 *
 * Spec traces:
 *   - spec/API.md §Auth: Admin role has user-management access; Editor/Reader do not
 *   - spec/feature/FRONTEND_BASIC.md §Shell: Admin section (Users + Configurations +
 *     Peripherals links) renders ONLY when isAdmin is true; placed ABOVE the Account
 *     section which always renders for everyone; adminNav = [{label:"Users",href:"/admin/users"},
 *     {label:"Configurations",href:"/admin/conf"}, {label:"Peripherals",href:"/admin/peripherals"}]
 *   - spec/feature/FRONTEND_BASIC.md §Routing: the UI hides the admin-menu entry when
 *     the role is not Admin; write actions rendered only when role ∈ {Editor, Admin}
 *   - spec/feature/AUTH.md §Refresh & revoke: revoke fails closed on Redis
 *     unreachability (503 STORAGE_UNAVAILABLE) — the refresh token stays live, so the
 *     UI must not present a failed logout as a completed one
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { AppShell } from "./app-shell";
import type { Me } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Mock heavy dependencies that AppShell pulls in
// ---------------------------------------------------------------------------
// Stable spies shared with the hoisted vi.mock factories below, so the logout
// tests can assert on redirect / auth-clear / toast side effects.
const { mockReplace, mockClear, mockToast, mockApiFetch } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  mockClear: vi.fn(),
  mockToast: vi.fn(),
  mockApiFetch: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn() }),
  usePathname: () => "/validation",
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ApiError — mirror the real constructor signature (payload: ApiErrorPayload, status: number).
// toastApiError is NOT mocked: it imports ApiError from this same mocked module, so its
// instanceof checks stay consistent and the real toast pipeline is exercised.
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
  return { ApiError, apiFetch: mockApiFetch };
});

vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

// Mock useMe so we can control isAdmin without a real query
const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMe(),
}));

// Provide a minimal stub for useAuthStore
vi.mock("@/lib/auth/store", () => ({
  useAuthStore: (selector: (s: { clear: () => void }) => unknown) =>
    selector({ clear: mockClear }),
}));

// ThemeToggle uses next-themes internals — stub it out
vi.mock("@/components/theme-toggle", () => ({
  ThemeToggle: () => React.createElement("div", { "data-testid": "theme-toggle" }),
}));

// NotificationCenter uses TanStack Query — stub it out in shell unit tests
vi.mock("@/components/notification-center", () => ({
  NotificationCenter: () => React.createElement("div", { "data-testid": "notification-center" }),
}));

// getRuntimeConfig — supplies the deployment-local links (Airflow, ReDoc).
const mockGetRuntimeConfig = vi.fn();
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => mockGetRuntimeConfig(),
}));

// useDisplayLinks — supplies the peripheral-sourced links (DataHub, Langfuse),
// resolved env-first then from GET /spoke/common/peripheral-links.
const mockUseDisplayLinks = vi.fn();
vi.mock("@/lib/api/peripheral-links", () => ({
  useDisplayLinks: () => mockUseDisplayLinks(),
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

/**
 * Sets both resolution planes at once. The shell reads the peripheral-sourced
 * links (DataHub, Langfuse) from useDisplayLinks and the deployment-local ones
 * (Airflow, ReDoc) from getRuntimeConfig — deliberately distinct sources.
 */
function setInfraLinks(urls: {
  datahubUrl: string;
  langfuseUrl: string;
  langfuseProjectId?: string;
  airflowUrl: string;
  apiBaseUrl: string;
}): void {
  mockUseDisplayLinks.mockReturnValue({
    datahubUrl: urls.datahubUrl,
    langfuseUrl: urls.langfuseUrl,
    langfuseProjectId: urls.langfuseProjectId ?? "",
  });
  mockGetRuntimeConfig.mockReturnValue({
    airflowUrl: urls.airflowUrl,
    apiBaseUrl: urls.apiBaseUrl,
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockUseDisplayLinks.mockReset();
  mockReplace.mockReset();
  mockClear.mockReset();
  mockToast.mockReset();
  mockApiFetch.mockReset();
  mockApiFetch.mockResolvedValue(undefined);
  // Default: no infra URLs configured
  setInfraLinks({ datahubUrl: "", langfuseUrl: "", airflowUrl: "", apiBaseUrl: "" });
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

    // Validation is a flat link; MetaGen is a collapsible group (like Ingestion
    // and OntoGen) rendered as a toggle button with Config/Result/Uncovered
    // submenus underneath. Child labels use sentence case per the FRONTEND_BASIC.md
    // §Shell sidebar diagram; hrefs stay at the /metagen/{conf,result,uncovered} routes.
    expect(screen.getByRole("link", { name: /validation/i })).toBeTruthy();
    const metagenGroup = screen.getByRole("button", { name: /metagen/i });
    expect(metagenGroup).toBeTruthy();

    // Expanding the group reveals its Config/Result/Uncovered submenu links.
    fireEvent.click(metagenGroup);
    const confLinks = screen.getAllByRole("link", { name: /^config$/i });
    const metagenConfLink = confLinks.find(
      (el) => el.getAttribute("href") === "/metagen/conf",
    );
    expect(metagenConfLink).toBeTruthy();
  });
});

describe("AppShell — Ingestion nav group (FRONTEND_BASIC.md §Shell)", () => {
  it.each([["Admin"], ["Editor"], ["Reader"]] as const)(
    "renders the Ingestion group with Config and Unmanaged children for %s role",
    (role) => {
      // spec/feature/FRONTEND_BASIC.md §Shell: Ingestion ▾ with submenus Config
      // (/ingestion/conf) and Unmanaged (/ingestion/unmanaged). Child labels use
      // sentence case per the FRONTEND_BASIC.md §Shell sidebar diagram; hrefs are
      // unchanged.
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

      // The group renders as a disclosure button (no direct /ingestion link).
      const ingestionToggle = screen.getByRole("button", { name: /ingestion/i });
      expect(ingestionToggle).toBeTruthy();

      // Expand the group, then assert its child links.
      fireEvent.click(ingestionToggle);

      const confLink = screen.getByRole("link", { name: /^config$/i });
      expect(confLink.getAttribute("href")).toBe("/ingestion/conf");

      const unmanagedLink = screen.getByRole("link", { name: /^unmanaged$/i });
      expect(unmanagedLink.getAttribute("href")).toBe("/ingestion/unmanaged");
    },
  );

  it("places the Ingestion group between the Governance group and Validation in the DOM", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell fixes the entries, grouping, and order:
    // Governance ▾ · Ingestion ▾ · Validation · OntoGen ▾ · MetaGen.
    // Each ▾ group renders as a disclosure <button> and stays collapsed unless its
    // active route is open. Under the /validation pathname mock both the Governance
    // and Ingestion groups are collapsed, so each contributes its toggle <button>.
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
    const ingestionGroup = screen.getByRole("button", { name: /ingestion/i });
    const validation = screen.getByRole("link", { name: /validation/i });

    // DOCUMENT_POSITION_FOLLOWING (0x04): the argument node comes AFTER `this`.
    expect(
      governanceGroup.compareDocumentPosition(ingestionGroup) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      ingestionGroup.compareDocumentPosition(validation) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("AppShell — header home link", () => {
  // spec: FRONTEND_BASIC.md §Shell — the product name links to /governance/dashboard.
  beforeEach(() => {
    mockUseMe.mockReturnValue({
      me: makeMe("Reader"),
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });
  });

  it("renders the DataSpoke product name as a link to /governance/dashboard", () => {
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    const home = screen.getByRole("link", { name: "DataSpoke" });
    expect(home.getAttribute("href")).toBe("/governance/dashboard");
  });
});

describe("AppShell — Governance Datasets nav entry", () => {
  // spec: FRONTEND_GOVERNANCE.md §Datasets — a `Datasets` entry under the
  // Governance group links to /governance/datasets.
  it.each([["Admin"], ["Editor"], ["Reader"]] as const)(
    "exposes a Datasets link at /governance/datasets under the Governance group for %s",
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

      // Governance is a collapsible group; under the /validation pathname mock it
      // is collapsed, so expand it to reveal its children.
      const governanceToggle = screen.getByRole("button", { name: /governance/i });
      fireEvent.click(governanceToggle);

      const datasetsLink = screen.getByRole("link", { name: /^datasets$/i });
      expect(datasetsLink.getAttribute("href")).toBe("/governance/datasets");
    },
  );
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
    setInfraLinks({
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
    setInfraLinks({
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
    setInfraLinks({ datahubUrl: "", langfuseUrl: "", airflowUrl: "", apiBaseUrl: "" });

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

describe("AppShell — logout", () => {
  // spec/feature/AUTH.md §Refresh & revoke — POST /auth/token/revoke revokes the
  // refresh token; the flow fails closed on Redis unreachability (503
  // STORAGE_UNAVAILABLE). The refresh token is an HttpOnly cookie, so only the
  // backend can end the session: when revoke fails the session is still live and
  // the UI must not render a logged-out state over it.
  beforeEach(() => {
    mockUseMe.mockReturnValue({
      me: makeMe("Reader"),
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });
  });

  /** Open the account dropdown and click Logout. */
  async function clickLogout() {
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /test user/i }),
      new MouseEvent("pointerdown", { bubbles: true }),
    );
    const logout = await screen.findByText(/^logout$/i);
    fireEvent.click(logout);
  }

  it("clears auth state and redirects to /login when revoke succeeds", async () => {
    // spec/feature/AUTH.md §Refresh & revoke — a successful revoke ends the session
    // server-side, so the local state may be dropped and the user sent to /login.
    mockApiFetch.mockResolvedValue(undefined);

    await clickLogout();

    // Backstop: the revoke call must actually have been made, otherwise the
    // assertions below would pass on a component that never attempted logout.
    await waitFor(() =>
      expect(mockApiFetch).toHaveBeenCalledWith("/auth/token/revoke", { method: "POST" }),
    );
    await waitFor(() => expect(mockClear).toHaveBeenCalled());
    expect(mockReplace).toHaveBeenCalledWith("/login");
  });

  it("keeps the user signed in and shows one toast when revoke fails with 503 STORAGE_UNAVAILABLE", async () => {
    // spec/feature/AUTH.md §Refresh & revoke — Redis unreachable during revoke →
    // 503 STORAGE_UNAVAILABLE, and revoke retains the refresh cookie because the
    // token is still live server-side.
    // spec/feature/FRONTEND_BASIC.md §Auth — "only on success does it clear the
    // in-memory access token and navigate to /login — a failed revoke leaves the
    // session live, so the UI surfaces the error and keeps the user signed in
    // rather than showing a signed-out shell over a refresh cookie only the API
    // can clear."
    const { ApiError } = await import("@/lib/api/client");
    mockApiFetch.mockRejectedValue(
      new ApiError(
        {
          error_code: "STORAGE_UNAVAILABLE",
          message: "Token revocation store unavailable; revoke denied.",
          trace_id: "abcdef1234567890",
          resp_time: "2026-07-17T00:00:00Z",
        },
        503,
      ),
    );

    await clickLogout();

    // Backstop proving the failing revoke branch ran.
    await waitFor(() => expect(mockToast).toHaveBeenCalled());
    expect(mockClear).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();

    // Exactly one toast: a failed logout is a single security-relevant message.
    // Stacking a generic error toast on top of the explanatory one buries the
    // "you are still signed in" fact the user must act on.
    expect(mockToast).toHaveBeenCalledTimes(1);
    const failureToast = mockToast.mock.calls[0][0] as {
      title?: string;
      description?: string;
      variant?: string;
    };
    expect(failureToast.title).toBe("Logout failed");
    expect(failureToast.variant).toBe("destructive");
    // The description states the security fact in plain language and carries the
    // trace id for operator correlation.
    expect(failureToast.description).toMatch(/still signed in/i);
    expect(failureToast.description).toContain("(trace: abcdef12)");
  });

  it("keeps the user signed in when revoke fails with a network error", async () => {
    // Revoke takes no bearer credential (spec/API.md §Authorization — the refresh
    // cookie is the credential), so there is no 401 branch to distinguish: every
    // failure means the revocation is unconfirmed and the session may still be
    // live. spec/feature/FRONTEND_BASIC.md §Auth — only success clears and
    // navigates. A non-ApiError throwable must take the same fail-closed path.
    mockApiFetch.mockRejectedValue(new TypeError("Failed to fetch"));

    await clickLogout();

    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    expect(mockClear).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();

    const failureToast = mockToast.mock.calls[0][0] as {
      title?: string;
      description?: string;
    };
    expect(failureToast.title).toBe("Logout failed");
    // No ApiError → no trace id to append; the security fact still stands alone.
    expect(failureToast.description).toMatch(/still signed in/i);
    expect(failureToast.description).not.toMatch(/trace:/);
  });
});
