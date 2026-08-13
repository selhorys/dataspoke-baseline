/**
 * Tests for MetricDatasetTable — the metric detail page's Datasets panel.
 *
 * Spec traces (spec/feature/FRONTEND_GOVERNANCE.md §Metric detail, Datasets panel):
 *   - "It binds to `GET .../dataset` with columns `dataset_urn` (linked to
 *     `/data/[urn]`), `datahub` (the shared DataHub dataset deep-link), a `met`
 *     badge (`true` / `false` / `unknown`), and `last check time` (shared
 *     tz/datetime helper; em dash when the row is `unknown`)."
 *   - "A three-way toggle group — true / false / unknown, all on by default —
 *     drives the repeatable `met` query param, resetting `offset` on change."
 *   - "With **zero** toggles selected the client renders the empty state and
 *     issues **no request**."
 *   - "The shared Pagination drives `offset`/`limit` with `sort=dataset_urn`."
 *   - "Beneath the table a muted line states the envelope's `attrs_synced_at` as
 *     the scope's freshness."
 *   - spec/API.md §Metric — `GET /spoke/governance/metric/{id}/dataset`: `met` is
 *     `"unknown"` when the dataset is in scope but carries no verdict;
 *     `attrs_synced_at` is scope-relative and "unaffected by `met` filtering or
 *     paging".
 *
 * Mocked: `useMetricDatasets` (the read under test is the *request* the panel
 * issues, not the transport — the URL that hook builds is covered in
 * lib/api/governance.test.ts, including that `enabled: false` fires no fetch),
 * `next/link` (href assertions in jsdom), peripheral-links (DataHub base URL),
 * and the display timezone (host-independent timestamps).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import React from "react";
import { MetricDatasetTable } from "./metric-dataset-table";
import { formatDateTime } from "@/lib/format-time";
import type { MetricDatasetListResponse, MetricDatasetRow } from "@/types/governance";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

const DATAHUB_URL = "https://datahub.example.test";
vi.mock("@/lib/api/peripheral-links", () => ({
  PERIPHERAL_LINKS_QUERY_KEY: ["peripheral-links"],
  useDisplayLinks: () => ({
    datahubUrl: DATAHUB_URL,
    langfuseUrl: "",
    langfuseProjectId: "",
  }),
}));

const mockUseMetricDatasets = vi.fn();
vi.mock("@/lib/api/governance", () => ({
  useMetricDatasets: (...args: unknown[]) => mockUseMetricDatasets(...args),
}));

// ── Fixtures (inline, readable) ────────────────────────────────────────────────

const METRIC_ID = "doc-health-dev";

const ORDERS = "urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,DEV)";
const CARRIERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.carriers,DEV)";
const ARCHIVE = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.archive,DEV)";

const ROWS: MetricDatasetRow[] = [
  { dataset_urn: ORDERS, met: "true", last_check_at: "2026-04-25T03:00:00Z", detail: {} },
  { dataset_urn: CARRIERS, met: "false", last_check_at: "2026-04-25T03:00:00Z", detail: {} },
  // In scope but never evaluated — no verdict, so no evidence time either.
  { dataset_urn: ARCHIVE, met: "unknown", last_check_at: null, detail: null },
];

const SYNCED_AT = "2026-04-25T02:00:00Z";

function response(overrides: Partial<MetricDatasetListResponse> = {}): MetricDatasetListResponse {
  return {
    offset: 0,
    limit: 20,
    total_count: ROWS.length,
    datasets: ROWS,
    attrs_synced_at: SYNCED_AT,
    ...overrides,
  };
}

/** The params the panel handed the read on its most recent render. */
function lastRequest(): {
  metricId: string;
  params: { met: string[]; offset: number; limit: number; sort: string };
  options: { enabled?: boolean };
} {
  const call = mockUseMetricDatasets.mock.calls.at(-1) as unknown[];
  return {
    metricId: call[0] as string,
    params: call[1] as { met: string[]; offset: number; limit: number; sort: string },
    options: (call[2] ?? {}) as { enabled?: boolean },
  };
}

function verdictToggle(verdict: string): HTMLElement {
  return screen.getByRole("checkbox", { name: verdict });
}

beforeEach(() => {
  mockUseMetricDatasets.mockReset();
  mockUseMetricDatasets.mockReturnValue({ data: response(), isLoading: false, error: null });
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

// ── Columns ────────────────────────────────────────────────────────────────────

describe("MetricDatasetTable — columns", () => {
  it("renders the four spec'd column headers", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    for (const header of ["dataset_urn", "datahub", "met criterion", "last check time"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
  });

  it("links each dataset_urn to its per-dataset hub page", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    expect(screen.getByRole("link", { name: ORDERS })).toHaveAttribute(
      "href",
      `/data/${encodeURIComponent(ORDERS)}`,
    );
  });

  it("renders the shared DataHub deep-link in the datahub cell", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    const row = screen.getByRole("row", { name: new RegExp(escapeRegExp(ORDERS)) });
    const datahubLink = within(row).getByRole("link", { name: /datahub/i });
    expect(datahubLink).toHaveAttribute(
      "href",
      `${DATAHUB_URL}/dataset/${encodeURIComponent(ORDERS)}`,
    );
  });

  it("renders each row's verdict as its badge text", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    for (const [urn, verdict] of [
      [ORDERS, "true"],
      [CARRIERS, "false"],
      [ARCHIVE, "unknown"],
    ] as const) {
      const row = screen.getByRole("row", { name: new RegExp(escapeRegExp(urn)) });
      expect(within(row).getByText(verdict)).toBeInTheDocument();
    }
  });

  it("renders last check time through the shared tz formatter", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    const row = screen.getByRole("row", { name: new RegExp(escapeRegExp(ORDERS)) });
    expect(
      within(row).getByText(formatDateTime("2026-04-25T03:00:00Z", "utc")),
    ).toBeInTheDocument();
  });

  it("renders an em dash as the last check time of an unknown row", () => {
    // spec/API.md §Metric — `unknown` means "in scope but never evaluated", so
    // there is no evidence time and no run to fall back to.
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    const row = screen.getByRole("row", { name: new RegExp(escapeRegExp(ARCHIVE)) });
    expect(within(row).getByText("—")).toBeInTheDocument();
  });
});

// ── Verdict toggle group → the repeatable `met` param ─────────────────────────

describe("MetricDatasetTable — three-way verdict toggle", () => {
  it("starts with all three verdicts selected and asks for all three", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    for (const verdict of ["true", "false", "unknown"]) {
      expect(verdictToggle(verdict)).toBeChecked();
    }
    expect(lastRequest().metricId).toBe(METRIC_ID);
    expect(lastRequest().params.met).toEqual(["true", "false", "unknown"]);
  });

  it("drops a deselected verdict from the `met` param and keeps the rest", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    fireEvent.click(verdictToggle("false"));

    expect(verdictToggle("false")).not.toBeChecked();
    expect(lastRequest().params.met).toEqual(["true", "unknown"]);
  });

  it("re-adds a verdict in the canonical true/false/unknown order", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    fireEvent.click(verdictToggle("true"));
    expect(lastRequest().params.met).toEqual(["false", "unknown"]);

    fireEvent.click(verdictToggle("true"));
    expect(lastRequest().params.met).toEqual(["true", "false", "unknown"]);
  });

  it("resets the offset when the selection changes", () => {
    // spec: the toggle group "drives the repeatable `met` query param, resetting
    // `offset` on change" — otherwise a narrowed result set can land the reader
    // on a page that no longer exists.
    mockUseMetricDatasets.mockReturnValue({
      data: response({ total_count: 45 }),
      isLoading: false,
      error: null,
    });
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    fireEvent.click(screen.getByRole("button", { name: "2" }));
    expect(lastRequest().params.offset).toBe(20);

    fireEvent.click(verdictToggle("unknown"));
    expect(lastRequest().params.offset).toBe(0);
    expect(lastRequest().params.met).toEqual(["true", "false"]);
  });

  it("issues no request and shows the empty state when no verdict is selected", () => {
    // spec: "an omitted repeatable param and an empty one are the same HTTP
    // request, which the API reads as 'all three', so the no-selection case
    // cannot be expressed on the wire and is resolved client-side instead."
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    for (const verdict of ["true", "false", "unknown"]) {
      fireEvent.click(verdictToggle(verdict));
    }

    expect(lastRequest().options.enabled).toBe(false);
    expect(screen.getByText(/no verdict selected/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("enables the read again as soon as one verdict comes back", () => {
    // Backstop for the assertion above: `enabled` is a live function of the
    // selection, not a flag stuck off after the first deselection.
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    for (const verdict of ["true", "false", "unknown"]) {
      fireEvent.click(verdictToggle(verdict));
    }
    expect(lastRequest().options.enabled).toBe(false);

    fireEvent.click(verdictToggle("false"));

    expect(lastRequest().options.enabled).toBe(true);
    expect(lastRequest().params.met).toEqual(["false"]);
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

// ── Paging ─────────────────────────────────────────────────────────────────────

describe("MetricDatasetTable — pagination and sort", () => {
  it("requests the dataset_urn sort", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    expect(lastRequest().params.sort).toBe("dataset_urn");
  });

  it("pages on offset/limit against the envelope's total_count", () => {
    mockUseMetricDatasets.mockReturnValue({
      data: response({ total_count: 45 }),
      isLoading: false,
      error: null,
    });
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    expect(lastRequest().params.offset).toBe(0);
    expect(lastRequest().params.limit).toBe(20);

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(lastRequest().params.offset).toBe(20);
    expect(lastRequest().params.limit).toBe(20);
  });
});

// ── Scope freshness + empty scope ──────────────────────────────────────────────

describe("MetricDatasetTable — scope freshness line", () => {
  it("states the envelope's attrs_synced_at beneath the table", () => {
    render(<MetricDatasetTable metricId={METRIC_ID} />);
    expect(
      screen.getByText(`Scope synced ${formatDateTime(SYNCED_AT, "utc")}`),
    ).toBeInTheDocument();
  });

  it("says the scope has never synced when attrs_synced_at is null", () => {
    // spec: the line exists so "an empty or unexpectedly small table is readable
    // as a pending sync rather than as a filter that matches nothing".
    mockUseMetricDatasets.mockReturnValue({
      data: response({ datasets: [], total_count: 0, attrs_synced_at: null }),
      isLoading: false,
      error: null,
    });
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    expect(screen.getByText(/never synced/i)).toBeInTheDocument();
  });

  it("shows a no-match empty state when the scope yields no row for the selection", () => {
    mockUseMetricDatasets.mockReturnValue({
      data: response({ datasets: [], total_count: 0 }),
      isLoading: false,
      error: null,
    });
    render(<MetricDatasetTable metricId={METRIC_ID} />);

    expect(screen.getByText(/no dataset in this metric's scope/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

/** Escapes a URN for use inside an accessible-name RegExp. */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
