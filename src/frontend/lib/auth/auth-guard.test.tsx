/**
 * Tests for lib/auth/auth-guard.tsx
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Routing: route guards redirect to /login when no access
 *     token; public routes (/login, /register, /forgot-password, /reset-password) are exempt;
 *     renders spinner while auth probe is pending; renders children when token present.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";
import { AuthGuard } from "./auth-guard";
import { useAuthStore } from "./store";

// ---------------------------------------------------------------------------
// Mock next/navigation
// ---------------------------------------------------------------------------
const mockReplace = vi.fn();
const mockPathname = vi.fn(() => "/ingestion");

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => mockPathname(),
}));

beforeEach(() => {
  mockReplace.mockClear();
  useAuthStore.setState({ accessToken: null, me: null, authInitialized: false });
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe("AuthGuard — loading state (authInitialized = false)", () => {
  it("renders a spinner while the auth probe has not completed", () => {
    useAuthStore.setState({ authInitialized: false, accessToken: null });

    render(
      <AuthGuard>
        <div data-testid="protected">Protected content</div>
      </AuthGuard>,
    );

    // Spinner is a div with animate-spin class
    const spinner = document.querySelector(".animate-spin");
    expect(spinner).toBeTruthy();
  });

  it("does NOT render children while auth is uninitialized", () => {
    useAuthStore.setState({ authInitialized: false, accessToken: null });

    render(
      <AuthGuard>
        <div data-testid="protected">Protected content</div>
      </AuthGuard>,
    );

    expect(screen.queryByTestId("protected")).toBeNull();
  });

  it("does NOT call router.replace while auth is uninitialized", () => {
    useAuthStore.setState({ authInitialized: false, accessToken: null });

    render(
      <AuthGuard>
        <div>content</div>
      </AuthGuard>,
    );

    expect(mockReplace).not.toHaveBeenCalled();
  });
});

describe("AuthGuard — authenticated (authInitialized = true, token present)", () => {
  it("renders children when the user has a token", () => {
    useAuthStore.setState({ authInitialized: true, accessToken: "valid-token" });

    render(
      <AuthGuard>
        <div data-testid="protected">Protected content</div>
      </AuthGuard>,
    );

    expect(screen.getByTestId("protected")).toBeTruthy();
  });

  it("does NOT redirect when the user has a token", () => {
    useAuthStore.setState({ authInitialized: true, accessToken: "valid-token" });

    render(
      <AuthGuard>
        <div>content</div>
      </AuthGuard>,
    );

    expect(mockReplace).not.toHaveBeenCalled();
  });
});

describe("AuthGuard — unauthenticated (authInitialized = true, no token)", () => {
  it("calls router.replace('/login?next=<encoded-path>') when no token", async () => {
    mockPathname.mockReturnValue("/ingestion");
    useAuthStore.setState({ authInitialized: true, accessToken: null });

    render(
      <AuthGuard>
        <div data-testid="protected">Protected content</div>
      </AuthGuard>,
    );

    // useEffect fires after render — flush it
    await act(async () => {});

    expect(mockReplace).toHaveBeenCalledWith(
      `/login?next=${encodeURIComponent("/ingestion")}`,
    );
  });

  it("renders no children when unauthenticated", async () => {
    useAuthStore.setState({ authInitialized: true, accessToken: null });

    render(
      <AuthGuard>
        <div data-testid="protected">Protected content</div>
      </AuthGuard>,
    );

    await act(async () => {});

    expect(screen.queryByTestId("protected")).toBeNull();
  });

  it("encodes a path with slashes correctly in the next param", async () => {
    mockPathname.mockReturnValue("/governance/dashboard");
    useAuthStore.setState({ authInitialized: true, accessToken: null });

    render(
      <AuthGuard>
        <div>content</div>
      </AuthGuard>,
    );

    await act(async () => {});

    expect(mockReplace).toHaveBeenCalledWith(
      `/login?next=${encodeURIComponent("/governance/dashboard")}`,
    );
  });
});
