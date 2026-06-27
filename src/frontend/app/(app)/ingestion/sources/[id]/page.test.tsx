/**
 * Tests for the ingestion source detail page — role/mode write-gating matrix.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail:
 *   - DATAHUB_MANAGED: recipe read-only, no Edit/Delete (DataHub is SSOT).
 *   - ACTIVE + Editor: Edit, Delete, and a runnable Run control.
 *   - Reader: no write controls.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import React from "react";
import IngestionSourceDetailPage from "./page";
import type { IngestionSource } from "@/types/ingestion";

// ── Mocks ──────────────────────────────────────────────────────────────────────
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mockSource = vi.fn();
const noop = () => ({
  mutate: vi.fn(),
  isPending: false,
  error: null,
  data: undefined,
});
vi.mock("@/lib/api/ingestion", () => ({
  useIngestionSource: () => mockSource(),
  useReplaceIngestionSource: () => noop(),
  useDeleteIngestionSource: () => noop(),
  useRunIngestionSource: () => noop(),
  useIngestionSourceDatasets: () => ({ data: { datasets: [], total_count: 0 } }),
  useIngestionSourceEvents: () => ({ data: { events: [], total_count: 0 } }),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

function makeSource(mode: IngestionSource["mode"]): IngestionSource {
  return {
    id: "src-1",
    mode,
    name: "test source",
    schedule: mode === "PASSIVE" ? null : "0 0 * * *",
    recipe: { source: { type: "postgres", config: {} } },
    platform: "postgres",
    status: "OK",
    datahub_source_urn: mode === "DATAHUB_MANAGED" ? "urn:li:dataHubIngestionSource:x" : null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-02T00:00:00Z",
  };
}

async function renderPage() {
  const params = Promise.resolve({ id: "src-1" });
  // `use(params)` suspends until the promise resolves; render inside an awaited
  // act() so the suspended promise flushes before assertions run.
  await act(async () => {
    render(
      <React.Suspense fallback={<div data-testid="suspense-fallback" />}>
        <IngestionSourceDetailPage params={params} />
      </React.Suspense>,
    );
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockSource.mockReset();
});

// The page uses `use(params)` which suspends; React Testing Library resolves
// the promise synchronously enough for these assertions via findBy queries.

describe("ingestion source detail — write gating", () => {
  it("ACTIVE + Editor shows Edit, Delete, and a Run control", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockSource.mockReturnValue({
      data: makeSource("ACTIVE_CUSTOM_MANAGED"),
      isLoading: false,
      error: null,
    });
    await renderPage();

    expect(await screen.findByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^run$/i })).toBeTruthy();
  });

  it("ACTIVE edit-mode swaps Edit/Delete for Save/Cancel and shows the authoring guide", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockSource.mockReturnValue({
      data: makeSource("ACTIVE_CUSTOM_MANAGED"),
      isLoading: false,
      error: null,
    });
    await renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /^edit$/i }));

    // Header now offers Save/Cancel; Edit/Delete are gone.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    // The collapsible secret-ref authoring guide is offered under the editor.
    expect(
      screen.getByText(/how to author a new source-credential reference/i),
    ).toBeTruthy();
  });

  it("DATAHUB_MANAGED + Editor hides Edit/Delete and shows the read-only note", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockSource.mockReturnValue({
      data: makeSource("DATAHUB_MANAGED"),
      isLoading: false,
      error: null,
    });
    await renderPage();

    expect((await screen.findAllByText(/source of truth/i)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    // Run not applicable for DATAHUB_MANAGED.
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();
  });

  it("ACTIVE + Reader hides all write controls", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockSource.mockReturnValue({
      data: makeSource("ACTIVE_CUSTOM_MANAGED"),
      isLoading: false,
      error: null,
    });
    await renderPage();

    // Header renders the source name; no write buttons.
    expect(await screen.findByText("test source")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();
  });

  it("renders an error state on 404 INGESTION_SOURCE_NOT_FOUND", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockSource.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new ApiError(
        {
          error_code: "INGESTION_SOURCE_NOT_FOUND",
          message: "not found",
          trace_id: "00000000-0000-0000-0000-000000000000",
          resp_time: new Date().toISOString(),
        },
        404,
      ),
    });
    await renderPage();

    expect(await screen.findByText(/not found/i)).toBeTruthy();
  });
});
