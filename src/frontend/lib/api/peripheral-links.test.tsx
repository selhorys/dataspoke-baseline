/**
 * Tests for lib/api/peripheral-links.ts — the DataHub / Langfuse display links.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "`GET /spoke/common/peripheral-links`
 *     serves the `peripheral_config` DB plane, the **sole** source of `datahub_url`,
 *     `langfuse_url`, and `langfuse_project_id` — the client carries no alternative
 *     for these three values". Airflow and ReDoc are deployment-local and stay on
 *     the runtime config, so they are absent from this hook.
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "A resolved link is retained while the
 *     read refreshes and across a failed refresh, so a wired icon never flashes away
 *     and back; only a read that has never succeeded leaves the value unresolved."
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "Both peripheral values are re-checked
 *     in the client against the display-link safety rule … and a failing value
 *     resolves to `""` — the same 'render no link' state as an unset one."
 *   - spec/API.md §Data Resource — `GET /spoke/common/peripheral-links` returns
 *     `{datahub_url, langfuse_url, langfuse_project_id}`; an unconfigured
 *     peripheral yields `""`, which clients read as "render no link".
 *
 * The request de-duplication case has no spec line of its own: FRONTEND_BASIC.md
 * §Shared Component Notes only says `DatahubDatasetLink` is "Reused across the dataset
 * tables", and the one-request-per-page consequence is a design constraint stated in
 * the `peripheral-links.ts` module docstring. It is tested here because the reuse the
 * spec mandates is what makes fan-out possible.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderHook, waitFor, render, screen, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  useDisplayLinks,
  usePeripheralLinks,
  PERIPHERAL_LINKS_QUERY_KEY,
} from "./peripheral-links";

const mockApiFetch = vi.fn();
vi.mock("@/lib/api/client", () => ({
  apiFetch: (path: string) => mockApiFetch(path),
}));

function setApi(
  overrides: Partial<{
    datahub_url: string;
    langfuse_url: string;
    langfuse_project_id: string;
  }> = {},
): void {
  mockApiFetch.mockResolvedValue({
    resp_time: "2026-07-19T00:00:00.000Z",
    datahub_url: "",
    langfuse_url: "",
    langfuse_project_id: "",
    ...overrides,
  });
}

let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeEach(() => {
  mockApiFetch.mockReset();
  setApi();
  // The hook sets its own `retry` on the query, which overrides a client-level
  // default; only the delay between attempts is left to the client, so collapse
  // it to zero to keep the failure cases fast without changing attempt counts.
  queryClient = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  });
});

afterEach(() => {
  queryClient.clear();
});

/**
 * Renders the resolved links alongside the raw query state, so a test can wait
 * for the query to SETTLE before asserting.
 *
 * This matters: before the first response `useDisplayLinks` resolves every field
 * to `""`, so an assertion made on the first render says nothing about what the
 * response carried. Gating on `isSuccess` (and on the fetched payload) is what
 * makes each assertion below falsifiable.
 *
 * The query fields are destructured **inside** the render callback rather than
 * returned as the query object: TanStack Query tracks which result properties a
 * render reads and re-renders only when a tracked one changes, so a field first
 * touched from the test body would never refresh.
 *
 * `usePeripheralLinks` shares the module-level query key with `useDisplayLinks`,
 * so mounting both still issues exactly one request.
 */
function renderSettledLinks() {
  return renderHook(
    () => {
      const { data, isSuccess, isError, isFetching, refetch } = usePeripheralLinks();
      return { links: useDisplayLinks(), data, isSuccess, isError, isFetching, refetch };
    },
    { wrapper },
  );
}

// ── Endpoint wiring ─────────────────────────────────────────────────────────────

describe("usePeripheralLinks — endpoint", () => {
  it("calls GET /spoke/common/peripheral-links verbatim", async () => {
    renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    expect(mockApiFetch).toHaveBeenCalledWith("/spoke/common/peripheral-links");
  });
});

// ── Documented per-hook retry exception ─────────────────────────────────────────

describe("usePeripheralLinks — retries once, not twice", () => {
  // spec: spec/feature/FRONTEND_BASIC.md §Query Error Policy — "The shell's
  //   peripheral-links read (GET /spoke/common/peripheral-links) retries once
  //   rather than twice: a failed refresh is already absorbed by the
  //   retain-last-resolved rule (see Shell), so further attempts change nothing a
  //   user can observe."
  // This is one of the two exceptions the spec grants to the global policy
  // (which retries twice), so deleting the override on the grounds that the
  // global rule covers it would violate the spec silently.
  it("issues exactly two attempts for a failing read", async () => {
    mockApiFetch.mockRejectedValue(new Error("peripheral-links unavailable"));

    const { result } = renderHook(() => usePeripheralLinks(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(mockApiFetch).toHaveBeenCalledTimes(2);
  });
});

// ── Resolution from the peripheral_config DB plane ──────────────────────────────

describe("useDisplayLinks — resolves from GET /spoke/common/peripheral-links", () => {
  it("serves each of the three fields from the response body", async () => {
    setApi({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });

    const { result } = renderSettledLinks();

    // Gate: the query has settled AND its payload carries these values, so the
    // assertion below observes the post-response state rather than the initial
    // all-empty one (which would pass under a hook that ignored the response).
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });

    // spec: spec/feature/FRONTEND_BASIC.md §Shell — the peripheral_config DB
    //   plane is the sole source of datahub_url, langfuse_url and
    //   langfuse_project_id, so a DB-plane PATCH reaches the UI unmasked.
    expect(result.current.links).toEqual({
      datahubUrl: "https://datahub.imazon.example.com",
      langfuseUrl: "https://langfuse.imazon.example.com",
      langfuseProjectId: "imazon-project",
    });
  });

  it("resolves an unconfigured peripheral to '' (render no link)", async () => {
    setApi({
      datahub_url: "",
      langfuse_url: "",
      langfuse_project_id: "",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // spec: spec/API.md §Data Resource — "An unconfigured peripheral yields `""`,
    //   which clients read as 'render no link'."
    expect(result.current.links).toEqual({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });

  it("resolves per field — a configured DataHub alongside an unwired Langfuse", async () => {
    // Both sides seeded: one peripheral wired, one not, so a hook that blanked
    // or broadcast a single value across all three fields fails here.
    setApi({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "",
      langfuse_project_id: "",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.links).toEqual({
      datahubUrl: "https://datahub.imazon.example.com",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });

  it("resolves to '' while the first read is in flight rather than inventing a link", async () => {
    // A query that never settles: nothing but the pre-response state can satisfy
    // this, and that state must be the documented "render no link" one.
    mockApiFetch.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });

    expect(result.current).toEqual({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });

  it("resolves to '' when the read fails, so no broken link is rendered", async () => {
    mockApiFetch.mockRejectedValue(new Error("peripheral-links unavailable"));

    const { result } = renderSettledLinks();

    // Backstop: prove the failure branch actually ran; otherwise an all-empty
    // result would be indistinguishable from a query that never started.
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.links).toEqual({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });
});

// ── No competing plane can mask the DB ──────────────────────────────────────────

describe("useDisplayLinks — the DB plane is the sole source", () => {
  // spec: spec/feature/FRONTEND_BASIC.md §Shell — "`GET /spoke/common/peripheral-links`
  //   serves the `peripheral_config` DB plane, the **sole** source of `datahub_url`,
  //   `langfuse_url`, and `langfuse_project_id` — the client carries no alternative
  //   for these three values, so nothing can mask what the DB holds."
  //
  // Both planes a regression could reach for are seeded with a sentinel that the
  // API never returns: the injected runtime config (whose type carries no key for
  // these three — itself part of the invariant, hence the cast) and the
  // NEXT_PUBLIC_* build-time env. Every assertion below therefore has an
  // alternative value available to leak, so an "is empty"/"is the API value"
  // expectation is falsifiable rather than trivially true. Both sentinels are
  // themselves safe values (a well-formed http(s) URL, an alphanumeric slug), so
  // a leak survives the client's safe-URL guard and reaches the assertion.
  const SENTINEL_URL = "https://sentinel.other-plane.example.com";
  const SENTINEL_PROJECT_ID = "sentinel-other-plane";

  beforeEach(() => {
    (window as { __DATASPOKE_RUNTIME_CONFIG__?: unknown }).__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://api.test.example",
      airflowUrl: "http://airflow.test.example",
      datahubUrl: SENTINEL_URL,
      langfuseUrl: SENTINEL_URL,
      langfuseProjectId: SENTINEL_PROJECT_ID,
    };
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", SENTINEL_URL);
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_URL", SENTINEL_URL);
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_PROJECT_ID", SENTINEL_PROJECT_ID);
  });

  afterEach(() => {
    delete (window as { __DATASPOKE_RUNTIME_CONFIG__?: unknown }).__DATASPOKE_RUNTIME_CONFIG__;
    vi.unstubAllEnvs();
  });

  it("resolves to '' while the first read is in flight, ignoring both sentinels", () => {
    mockApiFetch.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });

    // Nothing has come from the DB plane yet, and the two alternative planes hold
    // a usable URL — so anything but "" here is a value the client invented.
    expect(result.current).toEqual({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });

  it("resolves to '' when the DB plane reports the peripherals unconfigured", async () => {
    setApi({ datahub_url: "", langfuse_url: "", langfuse_project_id: "" });

    const { result } = renderSettledLinks();

    // Backstop: the empty payload really landed, so the "" below is the DB plane
    // being honoured over the sentinels rather than the pre-response state.
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({
      datahub_url: "",
      langfuse_url: "",
      langfuse_project_id: "",
    });

    expect(result.current.links).toEqual({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    });
  });

  it("serves the DB-plane values, never the competing sentinels", async () => {
    setApi({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.links).toEqual({
      datahubUrl: "https://datahub.imazon.example.com",
      langfuseUrl: "https://langfuse.imazon.example.com",
      langfuseProjectId: "imazon-project",
    });
    // Stated separately so a partial leak (one field taken from the other plane)
    // is reported as the sole-source violation it is.
    expect(Object.values(result.current.links)).not.toContain(SENTINEL_URL);
    expect(Object.values(result.current.links)).not.toContain(SENTINEL_PROJECT_ID);
  });
});

// ── No flash: a landed value survives a refetch ─────────────────────────────────

describe("useDisplayLinks — a resolved link does not flash away", () => {
  // spec: spec/feature/FRONTEND_BASIC.md §Shell — "A resolved link is retained
  //   while the read refreshes and across a failed refresh, so a wired icon never
  //   flashes away and back; only a read that has never succeeded leaves the value
  //   unresolved."
  it("keeps serving the last response across a refetch", async () => {
    setApi({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.links.datahubUrl).toBe("https://datahub.imazon.example.com");

    // Force a refetch that never settles. While it is in flight the hook must
    // keep serving the value it already has — the spec's "retained while the read
    // refreshes", which is what stops a wired icon flashing away and back.
    mockApiFetch.mockReturnValue(new Promise(() => {}));
    await act(async () => {
      void result.current.refetch();
    });

    // Backstop: the refetch really is in flight, so this is not a no-op assertion.
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.links).toEqual({
      datahubUrl: "https://datahub.imazon.example.com",
      langfuseUrl: "https://langfuse.imazon.example.com",
      langfuseProjectId: "imazon-project",
    });
  });

  it("keeps serving the last response when a later refetch fails", async () => {
    setApi({
      datahub_url: "https://datahub.imazon.example.com",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    mockApiFetch.mockRejectedValue(new Error("peripheral-links unavailable"));
    let refetchFailed = false;
    await act(async () => {
      refetchFailed = (await result.current.refetch()).isError;
    });

    // Backstop: the refetch really failed, so the unchanged links below are the
    // spec's "retained … across a failed refresh" rather than a refetch that
    // never happened.
    expect(refetchFailed).toBe(true);
    expect(result.current.links).toEqual({
      datahubUrl: "https://datahub.imazon.example.com",
      langfuseUrl: "https://langfuse.imazon.example.com",
      langfuseProjectId: "imazon-project",
    });
  });
});

// ── Untrusted-value guard ───────────────────────────────────────────────────────

describe("useDisplayLinks — treats stored URLs as untrusted", () => {
  // spec: spec/API.md §Data Resource → Display-link safety — the rule bars
  //   non-http(s) schemes, userinfo authorities, whitespace / C0 controls / bidi
  //   marks, and unrooted path shapes. spec/feature/FRONTEND_BASIC.md §Shell
  //   requires the client to re-check the value "before they reach an anchor
  //   `href`", degrading a failing value to "".
  it.each([
    ["javascript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["vbscript:msgbox(1)"],
    // Userinfo spoofing: the effective host is evil.com, not the trusted prefix.
    ["https://trusted.example.com@evil.com"],
    // CR/LF and whitespace.
    ["https://evil.example.com\n/x"],
    ["https://evil .example.com"],
    // Bidi override can visually disguise the hostname.
    ["https://evil‮com.example"],
    ["not-a-url"],
    ["//protocol-relative.example.com"],
  ])("degrades %s from the API to an empty link", async (hostile) => {
    setApi({ datahub_url: hostile, langfuse_url: hostile });

    const { result } = renderSettledLinks();

    // Backstop: the hostile value really reached the client — the degradation is
    // the client's doing, not an artefact of an empty response.
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({
      datahub_url: hostile,
      langfuse_url: hostile,
    });

    expect(result.current.links.datahubUrl).toBe("");
    expect(result.current.links.langfuseUrl).toBe("");
  });

  it("rejects a project id that would escape its path segment", async () => {
    setApi({ langfuse_project_id: "../../etc/passwd" });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({
      langfuse_project_id: "../../etc/passwd",
    });

    // spec: spec/API.md §Data Resource → Display-link safety, Length row —
    //   project_id "is further restricted to an alphanumeric slug".
    expect(result.current.links.langfuseProjectId).toBe("");
  });

  it("accepts ordinary http(s) URLs with ports and paths", async () => {
    // The negative cases above cannot catch an over-strict guard on their own, so
    // this seeds the admissible side of the rule: lowercase http(s) scheme, host
    // plus numeric port, a `/`-rooted path, and an alphanumeric project slug.
    setApi({
      datahub_url: "http://datahub-gms.internal:8080/ui",
      langfuse_url: "https://langfuse.example.com",
      langfuse_project_id: "proj_1-abc",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.links).toEqual({
      datahubUrl: "http://datahub-gms.internal:8080/ui",
      langfuseUrl: "https://langfuse.example.com",
      langfuseProjectId: "proj_1-abc",
    });
  });
});

// ── Request de-duplication across rows ──────────────────────────────────────────

describe("useDisplayLinks — no per-row request fan-out", () => {
  it("issues ONE request for a table of many rows sharing the hook", async () => {
    setApi({ datahub_url: "https://datahub.imazon.example.com" });

    function Row() {
      const { datahubUrl } = useDisplayLinks();
      return React.createElement("span", { "data-testid": "row" }, datahubUrl);
    }
    function Table() {
      return React.createElement(
        "div",
        null,
        Array.from({ length: 25 }, (_unused, i) => React.createElement(Row, { key: i })),
      );
    }

    await act(async () => {
      render(React.createElement(Table), { wrapper });
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("row")[0].textContent).toBe(
        "https://datahub.imazon.example.com",
      );
    });

    // 25 mounted instances, one stable module-level query key → one fetch. See
    // the module docstring in peripheral-links.ts for why this is load-bearing.
    expect(screen.getAllByTestId("row")).toHaveLength(25);
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
    // The stable key is the mechanism; pin it so a per-instance key regression
    // is reported here rather than only as a request-count surprise.
    expect(queryClient.getQueryData(PERIPHERAL_LINKS_QUERY_KEY)).toMatchObject({
      datahub_url: "https://datahub.imazon.example.com",
    });
  });
});
