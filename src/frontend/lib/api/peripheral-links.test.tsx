/**
 * Tests for lib/api/peripheral-links.ts — the two-plane display-link merge.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shell: DataHub and Langfuse (URL +
 *     langfuse_project_id) resolve from the runtime config **first**, then from
 *     GET /spoke/common/peripheral-links. Airflow and ReDoc are deployment-local
 *     and stay runtime-config-only, so they are absent from this hook.
 *   - spec/API.md §Data Resource — GET /spoke/common/peripheral-links returns
 *     {datahub_url, langfuse_url, langfuse_project_id}; an unconfigured
 *     peripheral yields "" which clients read as "render no link".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderHook, waitFor, render, screen, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useDisplayLinks, usePeripheralLinks } from "./peripheral-links";

const mockApiFetch = vi.fn();
vi.mock("@/lib/api/client", () => ({
  apiFetch: (path: string) => mockApiFetch(path),
}));

const mockGetRuntimeConfig = vi.fn();
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => mockGetRuntimeConfig(),
}));

function setEnv(
  overrides: Partial<{
    datahubUrl: string;
    langfuseUrl: string;
    langfuseProjectId: string;
  }> = {},
): void {
  mockGetRuntimeConfig.mockReturnValue({
    apiBaseUrl: "",
    airflowUrl: "",
    datahubUrl: "",
    langfuseUrl: "",
    langfuseProjectId: "",
    ...overrides,
  });
}

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
  mockGetRuntimeConfig.mockReset();
  setEnv();
  setApi();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});

afterEach(() => {
  queryClient.clear();
});

// ── Endpoint wiring ─────────────────────────────────────────────────────────────

describe("usePeripheralLinks — endpoint", () => {
  it("calls GET /spoke/common/peripheral-links verbatim", async () => {
    renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    expect(mockApiFetch).toHaveBeenCalledWith("/spoke/common/peripheral-links");
  });
});

// ── Merge precedence: env wins ──────────────────────────────────────────────────

/**
 * Renders the merge alongside the raw query so a test can wait for the query to
 * SETTLE before asserting precedence.
 *
 * This matters: `useDisplayLinks` returns the env value while the query is in
 * flight (`data?.datahub_url ?? ""`), so a bare
 * `await waitFor(() => expect(result.current).toEqual({...env}))` is satisfied by
 * the very first render — before the API plane carries anything. The assertion
 * then never observes a state where both planes hold a value, and passes
 * identically under an inverted `api || env` merge. Gating on `isSuccess` (and on
 * the fetched payload) is what makes the precedence assertion falsifiable.
 *
 * `usePeripheralLinks` shares the module-level query key with `useDisplayLinks`,
 * so mounting both still issues exactly one request.
 */
function renderSettledLinks() {
  return renderHook(
    () => ({ links: useDisplayLinks(), query: usePeripheralLinks() }),
    { wrapper },
  );
}

describe("useDisplayLinks — resolution precedence (env-first, then API)", () => {
  it("env set + API set → env wins (existing chart installs stay unchanged)", async () => {
    setEnv({
      datahubUrl: "https://env-datahub.example.com",
      langfuseUrl: "https://env-langfuse.example.com",
      langfuseProjectId: "env-project",
    });
    setApi({
      datahub_url: "https://api-datahub.example.com",
      langfuse_url: "https://api-langfuse.example.com",
      langfuse_project_id: "api-project",
    });

    const { result } = renderSettledLinks();

    // Gate: the query has settled AND its payload carries the competing values,
    // so both planes are populated at the moment of the assertion below.
    await waitFor(() => expect(result.current.query.isSuccess).toBe(true));
    expect(result.current.query.data).toMatchObject({
      datahub_url: "https://api-datahub.example.com",
      langfuse_url: "https://api-langfuse.example.com",
      langfuse_project_id: "api-project",
    });

    // spec: spec/feature/FRONTEND_BASIC.md §Shell — "Peripheral links resolve
    //   env-first: an explicitly-set env value wins, and the API supplies the
    //   value when the env var is unset."
    expect(result.current.links).toEqual({
      datahubUrl: "https://env-datahub.example.com",
      langfuseUrl: "https://env-langfuse.example.com",
      langfuseProjectId: "env-project",
    });
  });

  it("env empty + API set → API supplies the value (DB-plane-only wiring)", async () => {
    setEnv();
    setApi({
      datahub_url: "https://api-datahub.example.com",
      langfuse_url: "https://api-langfuse.example.com",
      langfuse_project_id: "api-project",
    });

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });

    await waitFor(() => {
      expect(result.current).toEqual({
        datahubUrl: "https://api-datahub.example.com",
        langfuseUrl: "https://api-langfuse.example.com",
        langfuseProjectId: "api-project",
      });
    });
  });

  it("env set + API empty → env value survives (no flash to empty)", async () => {
    setEnv({
      datahubUrl: "https://env-datahub.example.com",
      langfuseUrl: "https://env-langfuse.example.com",
      langfuseProjectId: "env-project",
    });
    setApi();

    const { result } = renderSettledLinks();
    // Before the query resolves the env value is already present …
    expect(result.current.links.datahubUrl).toBe("https://env-datahub.example.com");

    // Gate on settlement, so the assertion below observes the resolved empty API
    // payload rather than the pre-fetch state (which would pass regardless).
    await waitFor(() => expect(result.current.query.isSuccess).toBe(true));
    expect(result.current.query.data).toMatchObject({
      datahub_url: "",
      langfuse_url: "",
      langfuse_project_id: "",
    });

    // … and an empty API response does not clear the env value.
    expect(result.current.links).toEqual({
      datahubUrl: "https://env-datahub.example.com",
      langfuseUrl: "https://env-langfuse.example.com",
      langfuseProjectId: "env-project",
    });
  });

  it("both empty → all links resolve empty (render no link)", async () => {
    setEnv();
    setApi();

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());

    await waitFor(() => {
      expect(result.current).toEqual({
        datahubUrl: "",
        langfuseUrl: "",
        langfuseProjectId: "",
      });
    });
  });

  it("mixes planes per field — env DataHub over API Langfuse", async () => {
    setEnv({ datahubUrl: "https://env-datahub.example.com" });
    setApi({
      datahub_url: "https://api-datahub.example.com",
      langfuse_url: "https://api-langfuse.example.com",
      langfuse_project_id: "api-project",
    });

    const { result } = renderSettledLinks();
    await waitFor(() => expect(result.current.query.isSuccess).toBe(true));

    // Per-field merge: env wins for datahubUrl, API supplies the two fields the
    // env plane leaves unset.
    expect(result.current.links).toEqual({
      datahubUrl: "https://env-datahub.example.com",
      langfuseUrl: "https://api-langfuse.example.com",
      langfuseProjectId: "api-project",
    });
  });
});

// ── Loading behaviour ───────────────────────────────────────────────────────────

describe("useDisplayLinks — while loading", () => {
  it("returns the env value immediately rather than flashing empty", () => {
    setEnv({ datahubUrl: "https://env-datahub.example.com" });
    // A query that never settles: only the env fallback can satisfy this.
    mockApiFetch.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });
    expect(result.current.datahubUrl).toBe("https://env-datahub.example.com");
  });
});

// ── Untrusted-value guard ───────────────────────────────────────────────────────

describe("useDisplayLinks — treats stored URLs as untrusted", () => {
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
    setEnv();
    setApi({ datahub_url: hostile, langfuse_url: hostile });

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());

    await waitFor(() => {
      expect(result.current.datahubUrl).toBe("");
      expect(result.current.langfuseUrl).toBe("");
    });
  });

  it("degrades a hostile env value too — the env plane is not exempt", async () => {
    setEnv({ datahubUrl: "javascript:alert(1)" });
    setApi();

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(result.current.datahubUrl).toBe(""));
  });

  it("rejects a project id that would escape its path segment", async () => {
    setEnv();
    setApi({ langfuse_project_id: "../../etc/passwd" });

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    await waitFor(() => expect(result.current.langfuseProjectId).toBe(""));
  });

  it("accepts ordinary http(s) URLs with ports and paths", async () => {
    setEnv();
    setApi({
      datahub_url: "http://datahub-gms.internal:8080/ui",
      langfuse_url: "https://langfuse.example.com",
      langfuse_project_id: "proj_1-abc",
    });

    const { result } = renderHook(() => useDisplayLinks(), { wrapper });

    await waitFor(() => {
      expect(result.current).toEqual({
        datahubUrl: "http://datahub-gms.internal:8080/ui",
        langfuseUrl: "https://langfuse.example.com",
        langfuseProjectId: "proj_1-abc",
      });
    });
  });
});

// ── Request de-duplication across rows ──────────────────────────────────────────

describe("useDisplayLinks — no per-row request fan-out", () => {
  it("issues ONE request for a table of many rows sharing the hook", async () => {
    setEnv();
    setApi({ datahub_url: "https://api-datahub.example.com" });

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
        "https://api-datahub.example.com",
      );
    });

    // 25 mounted instances, one stable module-level query key → one fetch.
    expect(screen.getAllByTestId("row")).toHaveLength(25);
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});
