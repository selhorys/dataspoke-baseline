/**
 * Tests for app/providers.tsx — the query client's global retry wiring.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "One retry policy is set
 *     globally on the query client and governs every read; per-hook overrides are
 *     the documented exception, not the norm."
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "Non-transient — no
 *     retry, fail immediately. Any 4xx, plus PERIPHERAL_NOT_CONFIGURED regardless
 *     of its 503 status … a retry chain cannot change the answer and only spends
 *     seconds of backoff on every query in the app before arriving at the same
 *     result."
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "Everything else —
 *     retried up to twice, then surfaced to the render site."
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "Failed mutations keep
 *     the TanStack default of no retry and surface as toasts."
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "Failing fast does not
 *     stop polling: a page on the standard 15 s refetchInterval keeps re-issuing
 *     the read, so it leaves an error or onboarding state on its own once the
 *     underlying condition clears — no reload, no manual retry control."
 *   - spec/feature/FRONTEND_BASIC.md §Peripherals (/admin/peripherals): an unwired
 *     DataHub answers 503 PERIPHERAL_NOT_CONFIGURED across most of the feature
 *     surface, and "none of them burns retry backoff on it"; saving the DataHub
 *     card clears the condition and "pages already open pick it up on their next
 *     poll".
 *
 * error-policy.test.ts covers the predicate in isolation. This suite drives reads
 * and writes through the provider tree the app actually mounts, so that dropping
 * `retry` from providers.tsx — or hoisting it onto `defaultOptions.mutations` —
 * fails here rather than passing silently.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import React from "react";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Providers } from "./providers";
import { QueryErrorState } from "@/components/query-error-state";
import { ApiError } from "@/lib/api/client";
import { PERIPHERAL_NOT_CONFIGURED, defaultQueryRetry } from "@/lib/api/error-policy";
import { usePoll } from "@/lib/hooks/use-poll";
import { useAuthStore } from "@/lib/auth/store";
import type { Me } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Leaf providers that need browser APIs jsdom does not model are stubbed; the
// QueryClientProvider under test is left untouched.
// ---------------------------------------------------------------------------
vi.mock("next-themes", () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
}));

vi.mock("@/components/ui/toaster", () => ({
  Toaster: () => null,
}));

vi.mock("@/lib/preferences/timezone", () => ({
  TimezoneHydration: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
  useToast: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function apiError(
  status: number,
  overrides: Partial<{ error_code: string; detail: Record<string, unknown> }> = {},
): ApiError {
  return new ApiError(
    {
      error_code: overrides.error_code ?? "SOME_ERROR",
      message: "Something went wrong",
      trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
      resp_time: "2026-07-01T00:00:00Z",
      ...(overrides.detail !== undefined ? { detail: overrides.detail } : {}),
    },
    status,
  );
}

/** 503 PERIPHERAL_NOT_CONFIGURED with detail.peripheral, as the API sends it. */
function peripheralError(): ApiError {
  return apiError(503, {
    error_code: PERIPHERAL_NOT_CONFIGURED,
    detail: { peripheral: "datahub" },
  });
}

const ADMIN: Me = {
  id: "u1",
  email: "admin@example.com",
  name: "Admin",
  role: "Admin",
  has_password: true,
  has_google: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/** The provider tree the app mounts, with its own QueryClient. */
const underProviders = (child: React.ReactNode) => <Providers>{child}</Providers>;

/**
 * Mounts one failing read inside the given provider tree and reports how many
 * times the queryFn ran. Only the *delay* between attempts is overridden — the
 * retry decision stays whatever the tree's client configured, which is what is
 * under test.
 */
async function attemptsUnder(
  wrap: (child: React.ReactNode) => React.ReactElement,
  error: unknown,
): Promise<number> {
  const queryFn = vi.fn().mockRejectedValue(error);

  function Probe() {
    const query = useQuery({
      queryKey: ["retry-probe"],
      queryFn,
      retryDelay: 0,
      meta: { handledInline: true },
    });
    return <div>{query.isError ? "call failed" : "call pending"}</div>;
  }

  render(wrap(<Probe />));
  await screen.findByText("call failed");

  return queryFn.mock.calls.length;
}

/** The mutation counterpart of attemptsUnder, on the same provider tree. */
async function mutationAttemptsUnder(
  wrap: (child: React.ReactNode) => React.ReactElement,
  error: unknown,
): Promise<number> {
  const mutationFn = vi.fn().mockRejectedValue(error);

  function Probe() {
    const { mutate, isError } = useMutation<unknown, unknown, void>({
      mutationFn: () => mutationFn(),
      retryDelay: 0,
      meta: { handledInline: true },
    });
    React.useEffect(() => {
      mutate();
    }, [mutate]);
    return <div>{isError ? "call failed" : "call pending"}</div>;
  }

  render(wrap(<Probe />));
  await screen.findByText("call failed");

  return mutationFn.mock.calls.length;
}

beforeEach(() => {
  useAuthStore.setState({ me: null });
  // SilentRefresh probes POST /auth/token/refresh on mount; answer it so the
  // effect settles without touching the network.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      statusText: "Unauthorized",
      json: () => Promise.resolve({}),
    } as unknown as Response),
  );
});

// ---------------------------------------------------------------------------
// 1. The wiring itself
// ---------------------------------------------------------------------------

describe("Providers — the shared query client carries the global retry policy", () => {
  it("sets the error-policy retry as the default for every read", () => {
    // spec §Query Error Policy: "One retry policy is set globally on the query
    // client and governs every read."
    //
    // Deliberately an identity check rather than `typeof === "function"`: "one
    // retry policy" is a single-source claim, so the render path must use the
    // very function lib/api/error-policy.ts exports and not a local
    // re-implementation that happens to agree today. The attempt counts below
    // carry the behavioural half of the contract, so a behaviour-preserving
    // wrapper tripping this assertion is the intended, narrow strictness.
    let captured: QueryClient | null = null;

    function Probe() {
      captured = useQueryClient();
      return null;
    }

    render(
      <Providers>
        <Probe />
      </Providers>,
    );

    expect(captured).not.toBeNull();
    expect(captured!.getDefaultOptions().queries?.retry).toBe(defaultQueryRetry);
  });
});

// ---------------------------------------------------------------------------
// 2. Attempt counts for a read mounted inside Providers
// ---------------------------------------------------------------------------

describe("Providers — a read fails fast on non-transient errors", () => {
  it("issues exactly one attempt for 503 PERIPHERAL_NOT_CONFIGURED", async () => {
    // This is the regression the change exists to prevent: on a deployment with
    // an unwired DataHub, every page paid a full retry chain of backoff before
    // it could render anything.
    const attempts = await attemptsUnder(underProviders, peripheralError());

    expect(attempts).toBe(1);
  });

  it("issues exactly one attempt for a 404", async () => {
    const attempts = await attemptsUnder(underProviders, apiError(404, { error_code: "NOT_FOUND" }));

    expect(attempts).toBe(1);
  });

  it("issues exactly one attempt for a 403", async () => {
    const attempts = await attemptsUnder(
      underProviders,
      apiError(403, { error_code: "READ_ONLY_ROLE" }),
    );

    expect(attempts).toBe(1);
  });

  it("retries a 500 up to twice — three attempts in total", async () => {
    // Backstop for the single-attempt cases above: retries are not switched off
    // wholesale, so a one-attempt result there is the policy at work.
    const attempts = await attemptsUnder(
      underProviders,
      apiError(500, { error_code: "INTERNAL_ERROR" }),
    );

    expect(attempts).toBe(3);
  });

  it("retries a network failure up to twice — three attempts in total", async () => {
    const attempts = await attemptsUnder(underProviders, new TypeError("Failed to fetch"));

    expect(attempts).toBe(3);
  });
});

// ---------------------------------------------------------------------------
// 3. Mutations are not covered by the read policy
// ---------------------------------------------------------------------------

describe("Providers — mutations keep the no-retry default", () => {
  // spec §Query Error Policy: "Failed mutations keep the TanStack default of no
  // retry and surface as toasts." Hoisting the read policy onto
  // defaultOptions.mutations would replay a POST/PUT body on every 5xx, and no
  // attempt-count assertion on the read side would notice.

  it("issues exactly one attempt for a 500 — the write is never replayed", async () => {
    const attempts = await mutationAttemptsUnder(
      underProviders,
      apiError(500, { error_code: "INTERNAL_ERROR" }),
    );

    expect(attempts).toBe(1);
  });

  it("issues exactly one attempt for a network failure", async () => {
    // The failure mode that most tempts a retry — the request may never have
    // reached the server — is exactly the one where replaying an unsafe write is
    // least acceptable.
    const attempts = await mutationAttemptsUnder(underProviders, new TypeError("Failed to fetch"));

    expect(attempts).toBe(1);
  });

  it("configures no mutation retry on the client at all", () => {
    // Backstop for the two counts above: they hold because the read policy was
    // never hoisted onto mutations, not because some mutation-specific override
    // happens to sit in the probe.
    let captured: QueryClient | null = null;

    function Probe() {
      captured = useQueryClient();
      return null;
    }

    render(
      <Providers>
        <Probe />
      </Providers>,
    );

    expect(captured!.getDefaultOptions().mutations?.retry).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 4. Failing fast does not stop polling
// ---------------------------------------------------------------------------

describe("Providers — usePoll keeps polling a read that failed fast", () => {
  // This covers the composition of usePoll's refetchInterval with the fail-fast
  // policy, not TanStack's interval implementation. Fail-fast is only acceptable
  // UX because the page self-heals: spec §Query Error Policy — "Failing fast does
  // not stop polling … it leaves an error or onboarding state on its own once the
  // underlying condition clears — no reload, no manual retry control" — and
  // §Peripherals — "pages already open pick it up on their next poll". The canary
  // is the obvious wrong reaction to fail-fast: gating the read behind
  // `enabled: !isError`, or suspending the interval once the query has errored.

  it("re-issues the read after the poll interval and clears the onboarding state when it succeeds", async () => {
    vi.useFakeTimers();
    try {
      useAuthStore.setState({ me: ADMIN });
      let peripheralWired = false;
      const queryFn = vi.fn(() =>
        peripheralWired ? Promise.resolve({ ok: true }) : Promise.reject(peripheralError()),
      );

      function Probe() {
        const query = usePoll<{ ok: boolean }>({
          queryKey: ["poll-probe"],
          queryFn,
          retryDelay: 0,
          meta: { handledInline: true },
        });
        if (query.isError) {
          return <QueryErrorState error={query.error} context="Failed to load metrics" />;
        }
        if (query.data) return <div>metrics loaded</div>;
        return <div>loading</div>;
      }

      render(
        <Providers>
          <Probe />
        </Providers>,
      );

      // First poll: one attempt, no retry chain, onboarding state rendered.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(queryFn).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("link")).toHaveAttribute("href", "/admin/peripherals");

      // Second poll: the interval keeps running even though the read failed.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(queryFn).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("link")).toBeInTheDocument();

      // An operator wires the peripheral; the next poll heals the page with no
      // reload and no manual retry control.
      peripheralWired = true;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      // The interval fires the read; a further tick lets its resolution land in
      // the rendered output.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(queryFn).toHaveBeenCalledTimes(3);
      expect(screen.getByText("metrics loaded")).toBeInTheDocument();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// 5. Contrast with the client-library default the policy displaces
// ---------------------------------------------------------------------------

describe("Providers — the policy, not the client-library default, decides", () => {
  it("a client left on TanStack's own default keeps retrying the same peripheral error", async () => {
    // Shows what the configured policy displaces: without it the identical error
    // costs a chain of attempts, each one further into exponential backoff.
    const bare = new QueryClient();
    const underBareClient = (child: React.ReactNode) => (
      <QueryClientProvider client={bare}>{child}</QueryClientProvider>
    );

    const attempts = await attemptsUnder(underBareClient, peripheralError());

    expect(attempts).toBeGreaterThan(1);
  });
});
