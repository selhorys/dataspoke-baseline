/**
 * Tests for components/query-error-state.tsx — the single inline render point
 * for a failed read.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "A page or panel that
 *     surfaces a failed read inline renders it through QueryErrorState."
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes (QueryErrorState):
 *     "When the error is PERIPHERAL_NOT_CONFIGURED, it names the peripheral from
 *     detail.peripheral and renders a muted onboarding state styled like the
 *     empty state, not the destructive error state … Admins are directed to
 *     /admin/peripherals and get a link there; non-admins are told to ask an
 *     administrator, with no link, because that route is Admin-gated and a link
 *     they cannot follow is worse than a sentence naming who can. The
 *     role-specific line is held until the role resolves, so an admin is never
 *     shown the non-admin wording."
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes (QueryErrorState):
 *     "For every other error it renders the ordinary destructive error state with
 *     the message from the API's error envelope."
 *   - spec/feature/FRONTEND_BASIC.md §Routing: /admin/peripherals is the Admin
 *     peripheral-connections page the onboarding state points back to.
 *
 * The component reads the role straight from the Zustand auth store, so these
 * tests seed the real store and need no QueryClientProvider.
 *
 * On copy: these assertions pin what the spec constrains — that the peripheral is
 * *named*, who is pointed where, and which branch is destructive — not the exact
 * sentences. The display labels themselves have one owner,
 * lib/api/error-policy.test.ts.
 *
 * `text-destructive` is asserted as a design-system token rather than as DOM
 * structure: it is the literal state the spec contrasts this branch against
 * ("not the destructive error state"), and swapping it back in is the exact
 * regression this change removes.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { QueryErrorState } from "./query-error-state";
import { ApiError } from "@/lib/api/client";
import { PERIPHERAL_NOT_CONFIGURED } from "@/lib/api/error-policy";
import { useAuthStore } from "@/lib/auth/store";
import type { Me, UserRole } from "@/lib/api/types";

// next/link needs no App Router context for an href assertion; mirror the
// passthrough anchor used by the other component suites.
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function apiError(
  status: number,
  overrides: Partial<{
    error_code: string;
    message: string;
    detail: Record<string, unknown>;
  }> = {},
): ApiError {
  return new ApiError(
    {
      error_code: overrides.error_code ?? "SOME_ERROR",
      message: overrides.message ?? "Something went wrong",
      trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
      resp_time: "2026-07-01T00:00:00Z",
      ...(overrides.detail !== undefined ? { detail: overrides.detail } : {}),
    },
    status,
  );
}

/** 503 PERIPHERAL_NOT_CONFIGURED with detail.peripheral, as the API sends it. */
function peripheralError(peripheral = "datahub"): ApiError {
  return apiError(503, {
    error_code: PERIPHERAL_NOT_CONFIGURED,
    message: "DataHub is not configured",
    detail: { peripheral },
  });
}

function me(role: UserRole): Me {
  return {
    id: "u1",
    email: "user@example.com",
    name: "User",
    role,
    has_password: true,
    has_google: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

beforeEach(() => {
  // Unresolved role is the store's own initial state; each test opts in to a role.
  useAuthStore.setState({ me: null });
});

// ---------------------------------------------------------------------------
// 1. Peripheral branch — Admin
// ---------------------------------------------------------------------------

describe("QueryErrorState — PERIPHERAL_NOT_CONFIGURED, Admin", () => {
  beforeEach(() => {
    useAuthStore.setState({ me: me("Admin") });
  });

  it("names the peripheral the envelope reports", () => {
    const { container } = render(
      <QueryErrorState error={peripheralError("datahub")} context="Failed to load metrics" />,
    );

    expect(container.textContent).toContain("DataHub");
  });

  it("names smtp when that is the peripheral the envelope reports", () => {
    const { container } = render(
      <QueryErrorState error={peripheralError("smtp")} context="Failed to send reset" />,
    );

    expect(container.textContent).toContain("SMTP");
  });

  it("directs the admin to Admin → Peripherals", () => {
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.getByText(/connect it in admin/i)).toBeInTheDocument();
  });

  it("renders a link, and it goes to /admin/peripherals", () => {
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    // getByRole throws on more than one match, so this also reads as "exactly one link".
    expect(screen.getByRole("link")).toHaveAttribute("href", "/admin/peripherals");
  });

  it("does NOT show the non-admin wording", () => {
    // spec: "an admin is never shown the non-admin wording".
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.queryByText(/ask an administrator/i)).not.toBeInTheDocument();
  });

  it("does NOT render the ordinary error message for the failed read", () => {
    // The onboarding branch replaces the destructive state; the raw envelope
    // message must not leak into it.
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.queryByText(/Failed to load metrics/)).not.toBeInTheDocument();
  });

  it("renders muted, not destructive", () => {
    // spec: "a muted onboarding state styled like the empty state, not the
    // destructive error state — an unwired peripheral is a setup step the
    // deployment has not reached".
    const { container } = render(
      <QueryErrorState error={peripheralError()} context="Failed to load metrics" />,
    );

    expect(container.querySelector(".text-destructive")).toBeNull();
  });

  it("still renders the onboarding state when the envelope carries no detail", () => {
    // Classification keys on the code alone (see error-policy.test.ts), so the
    // render branch must not fall back to the destructive state on a missing name.
    const { container } = render(
      <QueryErrorState
        error={apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED })}
        context="Failed to load metrics"
      />,
    );

    expect(screen.getByRole("link")).toHaveAttribute("href", "/admin/peripherals");
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(container.textContent).not.toMatch(/undefined|null/);
  });
});

// ---------------------------------------------------------------------------
// 2. Peripheral branch — known non-Admin roles
// ---------------------------------------------------------------------------

describe("QueryErrorState — PERIPHERAL_NOT_CONFIGURED, non-Admin roles", () => {
  const nonAdminRoles: UserRole[] = ["Editor", "Reader"];

  nonAdminRoles.forEach((role) => {
    it(`${role}: names the peripheral`, () => {
      useAuthStore.setState({ me: me(role) });
      const { container } = render(
        <QueryErrorState error={peripheralError()} context="Failed to load metrics" />,
      );

      expect(container.textContent).toContain("DataHub");
    });

    it(`${role}: is told to ask an administrator`, () => {
      useAuthStore.setState({ me: me(role) });
      render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

      expect(screen.getByText(/ask an administrator/i)).toBeInTheDocument();
    });

    it(`${role}: gets NO link, because /admin/peripherals is Admin-gated`, () => {
      useAuthStore.setState({ me: me(role) });
      render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });

    it(`${role}: does not get the admin wording`, () => {
      useAuthStore.setState({ me: me(role) });
      render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

      expect(screen.queryByText(/to use this page/)).not.toBeInTheDocument();
    });

    it(`${role}: renders muted, not destructive`, () => {
      useAuthStore.setState({ me: me(role) });
      const { container } = render(
        <QueryErrorState error={peripheralError()} context="Failed to load metrics" />,
      );

      expect(container.querySelector(".text-destructive")).toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------
// 3. Peripheral branch — role not yet resolved
// ---------------------------------------------------------------------------

describe("QueryErrorState — PERIPHERAL_NOT_CONFIGURED, role unresolved", () => {
  // spec: "The role-specific line is held until the role resolves, so an admin is
  // never shown the non-admin wording."

  it("names the peripheral so the user still learns why the page is empty", () => {
    const { container } = render(
      <QueryErrorState error={peripheralError()} context="Failed to load metrics" />,
    );

    expect(container.textContent).toContain("DataHub");
  });

  it("withholds the non-admin wording until the role is known", () => {
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.queryByText(/ask an administrator/i)).not.toBeInTheDocument();
  });

  it("withholds the admin wording and the link until the role is known", () => {
    render(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.queryByText(/connect it in admin/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the admin line and link once the role resolves to Admin", () => {
    // Backstop for the three assertions above: they are withheld pending the
    // role, not permanently absent.
    const { rerender } = render(
      <QueryErrorState error={peripheralError()} context="Failed to load metrics" />,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    useAuthStore.setState({ me: me("Admin") });
    rerender(<QueryErrorState error={peripheralError()} context="Failed to load metrics" />);

    expect(screen.getByText(/connect it in admin/i)).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "/admin/peripherals");
  });
});

// ---------------------------------------------------------------------------
// 4. Ordinary branch — every other error
// ---------------------------------------------------------------------------

describe("QueryErrorState — every other error renders the ordinary error state", () => {
  // spec §Shared Component Notes: "For every other error it renders the ordinary
  // destructive error state with the message from the API's error envelope."

  // The spec pins WHAT must be surfaced — the API envelope's `message` — not the
  // sentence it is composed into, so these assert the message is shown (alongside
  // the caller's context) rather than pinning "<context>: <message>" verbatim.

  it("shows the envelope message for an ApiError", () => {
    render(
      <QueryErrorState
        error={apiError(500, { error_code: "INTERNAL_ERROR", message: "Database connection failed" })}
        context="Failed to load metrics"
      />,
    );

    expect(screen.getByText(/Database connection failed/)).toBeInTheDocument();
    expect(screen.getByText(/Failed to load metrics/)).toBeInTheDocument();
  });

  it("shows the thrown message for a plain Error", () => {
    render(<QueryErrorState error={new Error("boom")} context="Failed to load metrics" />);

    expect(screen.getByText(/boom/)).toBeInTheDocument();
    expect(screen.getByText(/Failed to load metrics/)).toBeInTheDocument();
  });

  it("falls back to 'unknown error' for a throwable that is not an Error", () => {
    // No envelope and no Error message exist here, so the fallback wording is the
    // component's own; assert only that it degrades to a stated unknown-error copy.
    render(<QueryErrorState error={"just a string"} context="Failed to load metrics" />);

    expect(screen.getByText(/unknown error/i)).toBeInTheDocument();
  });

  it("an explicit message prop replaces the composed copy", () => {
    render(
      <QueryErrorState
        error={new Error("boom")}
        context="Failed to load metrics"
        message="Metrics are unavailable right now."
      />,
    );

    expect(screen.getByText("Metrics are unavailable right now.")).toBeInTheDocument();
    expect(screen.queryByText(/Failed to load metrics/)).not.toBeInTheDocument();
  });

  it("IS destructive", () => {
    // Backstop that keeps the onboarding branch's `.text-destructive` negatives
    // non-vacuous: the token is present on the branch the spec calls destructive,
    // so its absence there is a real contrast rather than a selector that never
    // matches anything.
    const { container } = render(
      <QueryErrorState error={new Error("boom")} context="Failed to load metrics" />,
    );

    expect(container.querySelector(".text-destructive")).not.toBeNull();
  });

  it("a 503 that is not PERIPHERAL_NOT_CONFIGURED stays on the ordinary branch", () => {
    // Backstop for the onboarding branch: the 503 status alone must not divert a
    // genuine outage into the setup-step wording.
    useAuthStore.setState({ me: me("Admin") });
    const { container } = render(
      <QueryErrorState
        error={apiError(503, { error_code: "STORAGE_UNAVAILABLE", message: "Postgres unreachable" })}
        context="Failed to load metrics"
      />,
    );

    expect(screen.getByText("Failed to load metrics: Postgres unreachable")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(container.querySelector(".text-destructive")).not.toBeNull();
  });

  it("renders the ordinary branch identically regardless of role", () => {
    // The role only shapes the onboarding branch; an ordinary failure reads the
    // same for everyone.
    useAuthStore.setState({ me: me("Reader") });
    const { unmount } = render(
      <QueryErrorState error={new Error("boom")} context="Failed to load metrics" />,
    );
    expect(screen.getByText("Failed to load metrics: boom")).toBeInTheDocument();
    unmount();

    useAuthStore.setState({ me: me("Admin") });
    render(<QueryErrorState error={new Error("boom")} context="Failed to load metrics" />);
    expect(screen.getByText("Failed to load metrics: boom")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
