/**
 * Tests for app/(app)/governance/metrics/[id]/page.tsx — the metric detail page.
 *
 * Scope: what the detail *page* owns, as distinct from the components it mounts
 * (each of which has its own file):
 *
 *   - the read-only Config panel's `metrics` render — "one line per series
 *     descriptor — a color swatch, the `name`, and its `idx` — in `idx` order"
 *   - `dataset_filter` rendered through DatasetFilterView (the SQL clause, not a
 *     four-dimension object)
 *   - the Datasets panel, mounted for this metric between Result and Event
 *   - which error slot a failed Save lands in: a `422 INVALID_DATASET_FILTER`
 *     goes inline against the filter field, everything else to the generic slot
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics (`/governance/metrics/[id]`):
 *     "`metrics` renders one line per series descriptor — a color swatch, the
 *     `name`, and its `idx` — in `idx` order."
 *   - same §: "`dataset_filter` is a SQL `WHERE`-clause string …, rendered through
 *     DatasetFilterView and edited through DatasetFilterEditor … A `422
 *     INVALID_DATASET_FILTER` from Save renders inline against the field."
 *   - same §: "The **Datasets** panel (`MetricDatasetTable` …) sits between the
 *     `Result` and `Event` panels".
 *   - same § route table: `/governance/metrics/[id]` reads
 *     `GET .../dataset?met&offset&limit&sort=dataset_urn` (the Datasets panel).
 *   - spec/API.md §Error catalogue — `INVALID_DATASET_FILTER`, 422, "`detail`
 *     carries the character position of the error".
 *
 * Mocked: the governance API hooks (this page's reads/writes are asserted through
 * the calls it makes, not the transport — the URLs are covered in
 * lib/api/governance.test.ts), `next/navigation`, `useMe`, the timezone, the
 * peripheral links (DataHub base URL for the Datasets panel's deep-links) and
 * recharts (jsdom has no layout for ResponsiveContainer). The real MetricForm,
 * DatasetFilterEditor/View and MetricDatasetTable are rendered, so the wiring
 * between page and component is under test rather than stubbed away.
 */

import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import React from "react";
import { act, render, screen, fireEvent, within } from "@testing-library/react";
import { ApiError } from "@/lib/api/client";
import type {
  MetricDatasetListResponse,
  MetricDefinition,
} from "@/types/governance";

// ── Mocks ──────────────────────────────────────────────────────────────────────

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/governance/metrics/doc-health-dev",
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

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

// recharts needs layout measurement jsdom does not provide.
vi.mock("recharts", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  return {
    ResponsiveContainer: Passthrough,
    LineChart: ({ children }: { children?: React.ReactNode }) => (
      <div data-testid="line-chart">{children}</div>
    ),
    Line: () => null,
    CartesianGrid: () => null,
    Legend: () => null,
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
  };
});

const mockConf = vi.fn();
const mockDatasets = vi.fn();
const replaceMutate = vi.fn();
/** Mutable so a test can put a server error on the PUT (Save) mutation. */
let replaceError: unknown = null;

vi.mock("@/lib/api/governance", () => ({
  useMetricConf: () => mockConf(),
  useMetricResults: () => ({ data: { results: [] }, isLoading: false, error: null }),
  useMetricEvents: () => ({
    data: { events: [], total_count: 0 },
    isLoading: false,
    error: null,
  }),
  useMetricDatasets: (...args: unknown[]) => mockDatasets(...args),
  useReplaceMetricConf: () => ({
    mutate: replaceMutate,
    isPending: false,
    error: replaceError,
  }),
  useUpdateMetricConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMetric: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRunMetric: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

import MetricDetailPage from "./page";

// ── Fixtures (inline, readable) ────────────────────────────────────────────────

const METRIC_ID = "doc-health-dev";

const ORDERS = "urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,DEV)";
const CARRIERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.carriers,DEV)";

// The composite clause spec/API.md §`dataset_filter` grammar prints as its worked
// example, stored across two lines so the view's line-break preservation shows.
const CLAUSE =
  "origin = 'DEV'\nAND ('urn:li:tag:area:catalog' IN tag_urns" +
  "\n     OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)";

function conf(overrides: Partial<MetricDefinition> = {}): MetricDefinition {
  return {
    id: METRIC_ID,
    mode: "active",
    is_enabled: true,
    metric_type: "doc-health",
    title: "Doc Health (DEV)",
    description: "Daily documentation-completeness check across DEV datasets",
    // Declared out of idx order so the view's ordering is observable.
    metrics: [
      { name: "total", color: "#64748B", idx: 2 },
      { name: "doc_health", color: "#A855F7", idx: 1 },
    ],
    metric_conf: {},
    schedule_tier: "daily",
    dataset_filter: CLAUSE,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-26T00:00:00Z",
    ...overrides,
  };
}

const DATASET_PAGE: MetricDatasetListResponse = {
  offset: 0,
  limit: 20,
  total_count: 2,
  datasets: [
    { dataset_urn: ORDERS, met: "true", last_check_at: "2026-04-25T03:00:00Z", detail: {} },
    { dataset_urn: CARRIERS, met: "false", last_check_at: "2026-04-25T03:00:00Z", detail: {} },
  ],
  attrs_synced_at: "2026-04-25T02:00:00Z",
};

async function renderPage() {
  const params = Promise.resolve({ id: METRIC_ID });
  await act(async () => {
    render(
      <React.Suspense fallback={<div data-testid="suspense-fallback" />}>
        <MetricDetailPage params={params} />
      </React.Suspense>,
    );
  });
}

/** True when `a` comes before `b` in document order. */
function precedes(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

/** The <section> whose heading is `name`. */
function section(name: string): HTMLElement {
  const heading = screen.getByRole("heading", { name, level: 2 });
  const el = heading.closest("section");
  expect(el, `no <section> around the ${name} heading`).not.toBeNull();
  return el as HTMLElement;
}

beforeAll(() => {
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
    Element.prototype.setPointerCapture = () => {};
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

beforeEach(() => {
  mockUseMe.mockReset();
  mockUseMe.mockReturnValue({ canWrite: true });
  mockConf.mockReset();
  mockConf.mockReturnValue({ data: conf(), isLoading: false, error: null });
  mockDatasets.mockReset();
  mockDatasets.mockReturnValue({ data: DATASET_PAGE, isLoading: false, error: null });
  replaceMutate.mockReset();
  replaceError = null;
});

// ── Config panel: the series descriptors ──────────────────────────────────────

describe("metric detail — `metrics` renders one line per series descriptor", () => {
  it("lists a swatch, the name and the idx for every descriptor", async () => {
    // spec/feature/FRONTEND_GOVERNANCE.md §Metrics: "`metrics` renders one line
    // per series descriptor — a color swatch, the `name`, and its `idx`".
    await renderPage();

    const lines = screen.getAllByRole("listitem");
    expect(lines).toHaveLength(2);

    for (const [name, idx, color] of [
      ["doc_health", "1", "#A855F7"],
      ["total", "2", "#64748B"],
    ] as const) {
      const line = lines.find((li) => within(li).queryByText(name) !== null);
      expect(line, `no line rendered for series ${name}`).toBeDefined();
      expect(within(line!).getByText(`(${idx})`)).toBeInTheDocument();
      // The swatch carries the descriptor's color as its own background —
      // the only place `color` is observable in the read-only view.
      const swatch = line!.querySelector("[aria-hidden='true']") as HTMLElement | null;
      expect(swatch, `series ${name} must render a color swatch`).not.toBeNull();
      expect(swatch!.style.backgroundColor).toBe(hexToRgb(color));
    }
  });

  it("orders the lines by idx, not by the order the API returned them", async () => {
    // The fixture declares total (idx 2) before doc_health (idx 1).
    await renderPage();
    const lines = screen.getAllByRole("listitem");
    expect(lines.map((li) => li.textContent)).toEqual([
      expect.stringContaining("doc_health"),
      expect.stringContaining("total"),
    ]);
  });
});

// ── Config panel: dataset_filter is the SQL clause ────────────────────────────

describe("metric detail — dataset_filter renders through DatasetFilterView", () => {
  it("renders the stored clause verbatim, line breaks preserved", async () => {
    // spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterView:
    // "a monospace `<pre>` block preserving the stored line breaks and indentation".
    await renderPage();

    // getByText's default normalizer collapses the very whitespace under test,
    // so read the block directly and compare its raw text.
    const filterField = screen.getByText("dataset_filter").closest("fieldset");
    expect(filterField).not.toBeNull();
    const block = (filterField as HTMLElement).querySelector("pre");
    expect(block, "the clause must render in a <pre> block").not.toBeNull();
    expect(block!.textContent).toBe(CLAUSE);
    expect(block!.className).toContain("font-mono");
  });

  it("renders an em dash for the all-datasets (empty) filter", async () => {
    // spec/API.md §`dataset_filter` grammar: "empty string = all registered datasets".
    mockConf.mockReturnValue({
      data: conf({ dataset_filter: "" }),
      isLoading: false,
      error: null,
    });
    await renderPage();

    const filterField = screen.getByText("dataset_filter").closest("fieldset");
    expect(filterField).not.toBeNull();
    expect(within(filterField as HTMLElement).getByText("—")).toBeInTheDocument();
  });
});

// ── The Datasets panel ────────────────────────────────────────────────────────

describe("metric detail — the Datasets panel", () => {
  it("mounts the covered-dataset table for this metric", async () => {
    // spec/feature/FRONTEND_GOVERNANCE.md §Metrics — the Datasets panel binds to
    // `GET .../dataset?met&offset&limit&sort=dataset_urn` for the metric in the route.
    await renderPage();

    const datasets = section("Datasets");
    expect(within(datasets).getByRole("columnheader", { name: "met criterion" })).toBeInTheDocument();
    expect(within(datasets).getByRole("link", { name: ORDERS })).toBeInTheDocument();

    const [metricId, params] = mockDatasets.mock.calls.at(-1) as [
      string,
      { met: string[]; sort: string },
    ];
    expect(metricId).toBe(METRIC_ID);
    expect(params.met).toEqual(["true", "false", "unknown"]);
    expect(params.sort).toBe("dataset_urn");
  });

  it("sits between the Result and Event panels", async () => {
    // spec/feature/FRONTEND_GOVERNANCE.md §Metrics — "The **Datasets** panel …
    // sits between the `Result` and `Event` panels".
    await renderPage();

    expect(precedes(section("Result"), section("Datasets"))).toBe(true);
    expect(precedes(section("Datasets"), section("Event"))).toBe(true);
  });
});

// ── Save errors land in exactly one slot ──────────────────────────────────────

describe("metric detail — a save error lands in exactly one slot", () => {
  /** Enters edit mode, where both error slots exist. */
  async function edit() {
    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    });
  }

  it("routes a 422 INVALID_DATASET_FILTER inline against the filter field, with its position", async () => {
    // spec/feature/FRONTEND_GOVERNANCE.md §Metrics: "A `422 INVALID_DATASET_FILTER`
    // from Save renders inline against the field."
    // spec/API.md §Error catalogue: "`detail` carries the character position".
    replaceError = new ApiError(
      {
        error_code: "INVALID_DATASET_FILTER",
        message: "unknown column 'owner' (at character 0)",
        trace_id: "t-1",
        resp_time: "2026-05-26T00:00:00Z",
        detail: { position: 0 },
      },
      422,
    );

    await edit();

    // The inline slot is the editor's own alert region, beside the textarea.
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("unknown column 'owner'");
    expect(alert.textContent).toContain("0");
    const editor = screen.getByLabelText("dataset_filter").closest("fieldset");
    expect(editor).not.toBeNull();
    expect(within(editor as HTMLElement).getByRole("alert")).toBe(alert);
  });

  it("keeps every other save error out of the filter field", async () => {
    // Backstop for the assertion above: the generic slot is real and is where a
    // non-filter error lands, so "inline only" is a routing decision, not the
    // page's only error surface.
    replaceError = new ApiError(
      {
        error_code: "METRIC_RUNNING",
        message: "a run is in flight",
        trace_id: "t-2",
        resp_time: "2026-05-26T00:00:00Z",
      },
      409,
    );

    await edit();

    expect(screen.getByText(/METRIC_RUNNING/)).toBeInTheDocument();
    const editor = screen.getByLabelText("dataset_filter").closest("fieldset");
    expect(within(editor as HTMLElement).queryByRole("alert")).toBeNull();
  });

  it("shows neither slot when the last save succeeded", async () => {
    await edit();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/METRIC_RUNNING/)).toBeNull();
  });
});

/** `#RRGGBB` → the `rgb(r, g, b)` form jsdom normalises inline styles to. */
function hexToRgb(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  // eslint-disable-next-line no-bitwise
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}
