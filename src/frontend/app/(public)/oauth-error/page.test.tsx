/**
 * Tests for the OAuth error page — /oauth-error.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §OAuth error page — copy is selected by
 *   lookup of the `error` query parameter into a fixed map; an absent or
 *   unrecognised code falls back to generic wording, and the received parameter
 *   value is never echoed into the rendered output. Every state carries a link
 *   back to /login, the only way onward from the page.
 * Spec: spec/feature/AUTH.md §Admin unbind — EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT
 *   renders the three-step recovery sequence (password reset → admin unlink →
 *   sign in with Google again).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import OAuthErrorPage from "./page";

// ── Mocks ──────────────────────────────────────────────────────────────────────
let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

function renderWith(query: string) {
  searchParams = new URLSearchParams(query);
  return render(<OAuthErrorPage />);
}

beforeEach(() => {
  searchParams = new URLSearchParams();
});

describe("OAuthErrorPage", () => {
  it("renders the three-step recovery sequence for EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT", () => {
    renderWith("error=EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT");

    expect(
      screen.getByRole("heading", { name: /linked to a different Google account/i }),
    ).toBeInTheDocument();

    const steps = screen.getAllByRole("listitem");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent(/password reset/i);
    expect(steps[1]).toHaveTextContent(/unlink/i);
    expect(steps[2]).toHaveTextContent(/[Ss]ign in with Google again/);
  });

  it("renders sibling-code copy without a recovery sequence", () => {
    renderWith("error=OAUTH_NOT_CONFIGURED");

    expect(
      screen.getByRole("heading", { name: /Google sign-in is not available/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/not configured on this deployment/i)).toBeInTheDocument();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("renders distinct copy for each of the five codes that reach the page", () => {
    const codes = [
      "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
      "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
      "OAUTH_STATE_MISMATCH",
      "OAUTH_EMAIL_NOT_VERIFIED",
      "OAUTH_NOT_CONFIGURED",
    ];

    const headings = codes.map((code) => {
      const { unmount } = renderWith(`error=${code}`);
      const heading = screen.getByRole("heading").textContent ?? "";
      unmount();
      return heading;
    });

    expect(new Set(headings).size).toBe(codes.length);
    // None of the five catalogued codes may render the fallback, which the spec
    // reserves for an absent or unrecognised code. Identified by its body
    // wording — the spec states that, not a heading string.
    for (const code of codes) {
      const { unmount } = renderWith(`error=${code}`);
      expect(screen.queryByText(/Google sign-in could not be completed/i)).toBeNull();
      unmount();
    }
  });

  it("falls back to generic wording for an unrecognised code", () => {
    renderWith("error=SOMETHING_ELSE");

    expect(screen.getByRole("heading", { name: "Sign-in failed" })).toBeInTheDocument();
    expect(
      screen.getByText(/Google sign-in could not be completed/i),
    ).toBeInTheDocument();
  });

  it("falls back to generic wording for a code that names an inherited object member", () => {
    for (const code of ["toString", "constructor", "valueOf", "hasOwnProperty", "__proto__"]) {
      const { unmount } = renderWith(`error=${encodeURIComponent(code)}`);
      expect(screen.getByRole("heading", { name: "Sign-in failed" })).toBeInTheDocument();
      expect(screen.getByText(/Google sign-in could not be completed/i)).toBeInTheDocument();
      unmount();
    }
  });

  it("falls back to generic wording when the code is absent", () => {
    renderWith("");

    // The spec (FRONTEND_BASIC.md §OAuth error page) states the fallback by its
    // wording, not by a heading string, so assert the wording. The heading is a
    // secondary structural check only.
    expect(screen.getByText(/Google sign-in could not be completed/i)).toBeInTheDocument();
    expect(screen.getByRole("heading")).toBeInTheDocument();
  });

  it("never echoes the received parameter value into the output", () => {
    renderWith("error=%3Cimg+src%3Dx+onerror%3Dalert(1)%3E");

    expect(screen.getByRole("heading", { name: "Sign-in failed" })).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("onerror");
    expect(document.body.querySelector("img")).toBeNull();
  });

  it("links back to /login in every state", () => {
    for (const query of ["", "error=OAUTH_STATE_MISMATCH", "error=EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT"]) {
      const { unmount } = renderWith(query);
      expect(screen.getByRole("link", { name: /back to sign in/i })).toHaveAttribute(
        "href",
        "/login",
      );
      unmount();
    }
  });
});
