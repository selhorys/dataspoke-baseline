/**
 * Tests for the Governance Dashboard page — /governance/dashboard.
 *
 * Focus: the metric view controls (type multi-select, title search, title sort),
 * the cap disclosure, the two distinct empty states, and the pre-envelope
 * (loading / failed) paints the controls sit above.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard — "Metric view controls"
 * row:
 *   - "a row of three controls beneath the header narrows and orders the
 *     already-fetched enabled set entirely client-side" over "the same
 *     `GET /spoke/governance/metric` (filter `is_enabled=true`) read that backs
 *     the cards — **no request parameter**";
 *   - "a **metric-type filter** (checkbox-group multi-select … all selected by
 *     default; deselecting every type yields an empty set rather than falling
 *     back to all)";
 *   - "a **title search** (case-insensitive substring over each metric's
 *     `title`, inactive while blank)";
 *   - "a **title sort** (`Title A→Z` / `Title Z→A`, ascending by default) — the
 *     title is the metric's human-facing identifier and what the reader scans
 *     the grid by, so both controls key off it".
 * Spec: same section, "Cap disclosure" row — "When `total_count` exceeds the
 *   returned row count, a muted note above the grid states that only the first
 *   100 enabled metrics are shown and that the filter and sort apply to those
 *   100 only."
 * Spec: same section — "The grid carries two distinct empty states. With no
 *   enabled metrics at all it points at the Metrics page as the place to enable
 *   one. With enabled metrics present but none surviving the type filter and
 *   title search it points at the view controls instead."
 *
 * Mocking is at the boundary: the enabled-metrics read is a vi.fn (no network),
 * MetricCard is a marker (it owns its own per-card reads and has its own test),
 * and RangePicker / ChartGrainPicker are stubs (calendar + Radix Select
 * internals need DOM measurement jsdom lacks; both are covered by their own
 * component tests).
 *
 * WHAT THE ui/select STUB DOES AND DOES NOT PROVE: the sort control's option
 * values/labels, the forwarded `value`, and the resulting reordering are real
 * signal — they come from the page. The `combobox` / `option` ARIA roles are
 * hardcoded BY THE STUB (Radix's real Select cannot be opened under jsdom), so
 * no assertion here pins the real ARIA contract; that is pinned in a browser by
 * tests/e2e/ground/governance/dashboard-view-controls.spec.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import type { MetricDefinitionListItem } from "@/types/governance";
import { METRIC_VIEW_KEYS } from "@/lib/hooks/use-metric-view-selection";

// ── Mocks ──────────────────────────────────────────────────────────────────────

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

// Args are forwarded (not swallowed) so the parameters the page hands the
// enabled-metrics read are observable — see the "controls add no request
// parameter" block.
const mockUseEnabledMetrics = vi.fn();
vi.mock("@/lib/api/governance", () => ({
  useEnabledMetrics: (...args: unknown[]) => mockUseEnabledMetrics(...args),
}));

// The card owns its own latest/trend reads (components/governance/metric-card.test.tsx).
// Here it is a marker echoing the identity and the title the page ordered by.
vi.mock("@/components/governance/metric-card", () => ({
  MetricCard: ({ metric }: { metric: MetricDefinitionListItem }) =>
    React.createElement(
      "div",
      { "data-testid": "metric-card", "data-metric-id": metric.id },
      metric.title,
    ),
}));

vi.mock("@/components/range-picker", () => ({
  RangePicker: () => React.createElement("div", { "data-testid": "range-picker" }),
}));
vi.mock("@/components/chart-grain-picker", () => ({
  ChartGrainPicker: () =>
    React.createElement("div", { "data-testid": "chart-grain-picker" }),
}));

// Radix's Select opens through pointer-capture APIs jsdom does not implement,
// so every option is put statically in the DOM and choosing one fires
// onValueChange — the established repo pattern (components/chart-grain-picker.test.tsx).
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children?: React.ReactNode;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "sort-select-root", "data-value": value },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectTrigger: ({
    children,
    ...rest
  }: {
    children?: React.ReactNode;
    "aria-label"?: string;
  }) =>
    React.createElement(
      "button",
      { type: "button", role: "combobox", "aria-label": rest["aria-label"] },
      children,
    ),
  SelectValue: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("span", null, children),
  SelectContent: ({
    children,
    onValueChange,
  }: {
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "div",
      null,
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectItem: ({
    value,
    children,
    onValueChange,
  }: {
    value: string;
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "button",
      {
        type: "button",
        role: "option",
        "aria-selected": false,
        // Echoed so a test can compare the option it chose against the value the
        // page forwards back into the Select, without naming the enum itself.
        "data-value": value,
        onClick: () => onValueChange?.(value),
      },
      children,
    ),
}));

import GovernanceDashboardPage from "./page";

// ── Fixtures (inline literals; titles are the load-bearing values) ─────────────
//
// The two candidate sort keys DISAGREE, on purpose:
//   - ascending by `title`:       ALPHA ("Alpha …") < BRAVO ("Bravo …") < CHARLIE ("Charlie …");
//   - ascending by `description`: BRAVO ("Xray …") < ALPHA ("Yankee …") < CHARLIE ("Zulu …").
// The description order is a THIRD-ORDER PERMUTATION of the title order, not its
// reverse: it equals neither ascending nor descending title order, so ordering
// by `description` is caught whichever way the direction flag is mapped. (A
// reversed description order would let a description sort with an inverted
// direction slip through.)
// Every search needle likewise sits MID-`title` and appears in NO description,
// and each description opens with a token ("Yankee" / "Xray" / "Zulu") that
// appears in no title — so a search over `description` is detectable too.
// One metric per metric_type.

const ALPHA: MetricDefinitionListItem = {
  id: "alpha-freshness",
  mode: "active",
  is_enabled: true,
  metric_type: "ingestion-freshness",
  title: "Alpha Quebec Freshness",
  description: "Yankee ingest coverage across DEV datasets",
  metrics: ["total", "ingested_in_time"],
  metric_conf: { time_window_sec: 172800 },
  schedule_tier: "daily",
  dataset_filter: {},
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  last_run_at: null,
};

const BRAVO: MetricDefinitionListItem = {
  id: "bravo-validation",
  mode: "active",
  is_enabled: true,
  metric_type: "validation-score",
  // "ROMEO" is upper-case and mid-title, matched below by a lower-case needle;
  // "Tango" is shared with CHARLIE alone.
  title: "Bravo ROMEO Tango Validation",
  description: "Xray coverage on PROD runs",
  metrics: ["total", "validation_score_sum"],
  metric_conf: { time_window_sec: 172800 },
  schedule_tier: "daily",
  dataset_filter: {},
  created_at: "2026-05-02T00:00:00Z",
  updated_at: "2026-05-02T00:00:00Z",
  last_run_at: null,
};

const CHARLIE: MetricDefinitionListItem = {
  id: "charlie-doc-health",
  mode: "active",
  is_enabled: true,
  metric_type: "doc-health",
  // "sierra" mirrors ROMEO in the other case direction.
  title: "Charlie sierra Tango Doc Health",
  description: "Zulu documentation quality",
  metrics: ["total", "doc_health"],
  metric_conf: {},
  schedule_tier: "daily",
  dataset_filter: {},
  created_at: "2026-05-03T00:00:00Z",
  updated_at: "2026-05-03T00:00:00Z",
  last_run_at: null,
};

// ── Helpers ────────────────────────────────────────────────────────────────────

/** Seed the (mocked) enabled-metrics read. `totalCount` defaults to no cap. */
function setEnabledMetrics(
  metrics: MetricDefinitionListItem[],
  totalCount: number = metrics.length,
): void {
  mockUseEnabledMetrics.mockReturnValue({
    data: { offset: 0, limit: 100, total_count: totalCount, metrics },
    isLoading: false,
    error: null,
  });
}

async function renderPage(): Promise<void> {
  await act(async () => {
    render(<GovernanceDashboardPage />);
  });
}

/** Metric ids of the rendered cards, in DOM (display) order. */
function cardIds(): string[] {
  return screen
    .queryAllByTestId("metric-card")
    .map((el) => el.getAttribute("data-metric-id") as string);
}

/** Whitespace-normalized page text, for the cap-disclosure assertions. */
function pageText(): string {
  return (document.body.textContent ?? "").replace(/\s+/g, " ");
}

async function type(label: string, value: string): Promise<void> {
  await act(async () => {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  });
}

async function click(el: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(el);
  });
}

// Options are SELECTED by the ordering their label names, not by the impl's
// internal "asc"/"desc" enum, which the spec never mentions. The two patterns are
// disjoint: "Title Z→A" has no upper-case A before a Z, and vice versa. They say
// nothing about the rest of the label — the sort KEY the labels name is pinned
// separately, in "offers an ascending and a descending title order".
const ascOption = (): HTMLElement => screen.getByRole("option", { name: /A.*Z/ });
const descOption = (): HTMLElement => screen.getByRole("option", { name: /Z.*A/ });

beforeEach(() => {
  localStorage.clear();
  mockUseEnabledMetrics.mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

// ── Defaults ───────────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — view-control defaults", () => {
  it("checks every metric type, leaves the search blank, and orders ascending by title", async () => {
    setEnabledMetrics([CHARLIE, ALPHA, BRAVO]); // deliberately unsorted from the API
    await renderPage();

    // spec: "all selected by default".
    for (const t of ["ingestion-freshness", "validation-score", "doc-health"]) {
      expect(screen.getByRole("checkbox", { name: t })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    }
    // spec: the search is "inactive while blank" — it starts blank, so every
    // enabled metric is present.
    expect(screen.getByLabelText("Search titles")).toHaveValue("");
    // The prompt the reader actually sees has to name the field searched, not
    // only the accessible name: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard
    // renders the control as "[ Search titles…          ]". Matched on the
    // leading words, since the trailing ellipsis is the impl's typography.
    expect(
      screen.getByLabelText("Search titles").getAttribute("placeholder"),
    ).toMatch(/^Search titles\b/i);
    // spec: "ascending by default" — ordered by `title`, which here is neither
    // the arbitrary order the read returned nor ascending `description` order
    // (which here is [bravo, alpha, charlie] — neither title direction).
    expect(cardIds()).toEqual([
      "alpha-freshness",
      "bravo-validation",
      "charlie-doc-health",
    ]);
  });
});

// ── Type filter (multi-select) ─────────────────────────────────────────────────

describe("GovernanceDashboardPage — metric-type filter", () => {
  it("deselecting one type drops that type's cards and keeps the rest", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    await click(screen.getByRole("checkbox", { name: "validation-score" }));

    expect(cardIds()).toEqual(["alpha-freshness", "charlie-doc-health"]);
    expect(screen.getByRole("checkbox", { name: "validation-score" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("re-selecting the type brings its cards back", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    await click(screen.getByRole("checkbox", { name: "doc-health" }));
    expect(cardIds()).toEqual(["alpha-freshness", "bravo-validation"]);

    await click(screen.getByRole("checkbox", { name: "doc-health" }));
    expect(cardIds()).toEqual([
      "alpha-freshness",
      "bravo-validation",
      "charlie-doc-health",
    ]);
  });

  it("deselecting every type yields the filtered-empty state, not an implicit 'all'", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    for (const t of ["ingestion-freshness", "validation-score", "doc-health"]) {
      await click(screen.getByRole("checkbox", { name: t }));
    }

    // spec: "deselecting every type yields an empty set rather than falling back to all".
    expect(cardIds()).toEqual([]);
    // spec: with enabled metrics present, the empty state "points at the view
    // controls instead" — and specifically NOT at the Metrics page.
    expect(screen.getByText(/controls/i)).toBeInTheDocument();
    expect(screen.queryByText(/Metrics page/i)).not.toBeInTheDocument();
  });
});

// ── Title search ───────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — title search", () => {
  it("keeps only the metrics whose title contains the needle as a substring", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // "Quebec" appears mid-TITLE in ALPHA only and in no description — a
    // substring match, not a prefix and not an exact match.
    await type("Search titles", "Quebec");

    expect(cardIds()).toEqual(["alpha-freshness"]);
  });

  it("matches case-insensitively in both directions", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // Lowercase needle against BRAVO's upper-case, mid-title "ROMEO".
    await type("Search titles", "romeo");
    expect(cardIds()).toEqual(["bravo-validation"]);

    // Upper-case needle against CHARLIE's lower-case, mid-title "sierra".
    await type("Search titles", "SIERRA");
    expect(cardIds()).toEqual(["charlie-doc-health"]);
  });

  it("ignores the description — a token only a description carries matches nothing", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // Backstop: a title token narrows to one card, so the empty results below
    // are the search declining to read `description`, not an inert input.
    await type("Search titles", "Quebec");
    expect(cardIds()).toEqual(["alpha-freshness"]);

    // spec: the search is a substring "over each metric's `title`". "Yankee"
    // opens ALPHA's `description` and "Xray" opens BRAVO's; neither is in any
    // title, so a search that fell back to `description` would return a card.
    await type("Search titles", "Yankee");
    expect(cardIds()).toEqual([]);

    await type("Search titles", "Xray");
    expect(cardIds()).toEqual([]);
  });

  it("is inactive while blank or whitespace-only", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // Backstop: a real needle narrows first, so the "everything back" result
    // below is the blank search being inactive, not the input being ignored.
    await type("Search titles", "Quebec");
    expect(cardIds()).toEqual(["alpha-freshness"]);

    await type("Search titles", "   ");
    expect(screen.getByLabelText("Search titles")).toHaveValue("   ");
    expect(cardIds()).toEqual([
      "alpha-freshness",
      "bravo-validation",
      "charlie-doc-health",
    ]);
  });

  it("yields the filtered-empty state when nothing matches", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    await type("Search titles", "no-such-title");

    expect(cardIds()).toEqual([]);
    expect(screen.getByText(/controls/i)).toBeInTheDocument();
    expect(screen.queryByText(/Metrics page/i)).not.toBeInTheDocument();
  });
});

// ── Title sort ─────────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — title sort", () => {
  it("offers an ascending and a descending title order", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // spec: "`Title A→Z` / `Title Z→A`" — exactly those two orders, matched on
    // the ordering each label names.
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(ascOption()).toBeInTheDocument();
    expect(descOption()).toBeInTheDocument();

    // Each label must also NAME the key it sorts on: a control whose visible copy
    // still says "Description" while the grid orders by title misdescribes what
    // the reader is choosing. "Description" contains no A or Z, so the ordering
    // patterns above cannot catch that on their own.
    expect(ascOption()).toHaveTextContent(/^Title\b/);
    expect(descOption()).toHaveTextContent(/^Title\b/);
  });

  it("reverses the card order when toggled to descending, and restores it", async () => {
    setEnabledMetrics([CHARLIE, ALPHA, BRAVO]);
    await renderPage();

    expect(cardIds()).toEqual([
      "alpha-freshness",
      "bravo-validation",
      "charlie-doc-health",
    ]);

    const desc = descOption();
    await click(desc);
    expect(cardIds()).toEqual([
      "charlie-doc-health",
      "bravo-validation",
      "alpha-freshness",
    ]);
    // The control itself has to move with the selection, or it renders stale
    // (the real Select shows the label of whatever `value` it is handed).
    expect(screen.getByTestId("sort-select-root")).toHaveAttribute(
      "data-value",
      desc.getAttribute("data-value"),
    );

    const asc = ascOption();
    await click(asc);
    expect(cardIds()).toEqual([
      "alpha-freshness",
      "bravo-validation",
      "charlie-doc-health",
    ]);
    expect(screen.getByTestId("sort-select-root")).toHaveAttribute(
      "data-value",
      asc.getAttribute("data-value"),
    );
  });

  it("orders by title rather than by the read's own order or by description", async () => {
    // TITLE_VS_DESCRIPTION isolates the sort key on a three-card read. Three
    // cards, not two: with two cards any second key is either the same order or
    // its exact reverse, so a description sort could hide behind a flipped
    // direction. Here the description order is a third-order permutation —
    //   read order:      [zebra, aardvark, mango]
    //   title asc:       [aardvark, mango, zebra]   ← the expectation
    //   title desc:      [zebra, mango, aardvark]
    //   description asc: [mango, aardvark, zebra]
    //   description desc:[zebra, aardvark, mango]
    // — so only a `title` sort in the ascending direction produces it.
    const aardvarkTitleMikeDescription: MetricDefinitionListItem = {
      ...ALPHA,
      id: "aardvark-title",
      title: "Aardvark ingest freshness",
      description: "Mike ingest freshness",
    };
    const mangoTitleAardvarkDescription: MetricDefinitionListItem = {
      ...BRAVO,
      id: "mango-title",
      title: "Mango validation coverage",
      description: "Aardvark validation coverage",
    };
    const zebraTitleTangoDescription: MetricDefinitionListItem = {
      ...CHARLIE,
      id: "zebra-title",
      title: "Zebra documentation completeness",
      description: "Tango documentation completeness",
    };
    setEnabledMetrics([
      zebraTitleTangoDescription,
      aardvarkTitleMikeDescription,
      mangoTitleAardvarkDescription,
    ]);
    await renderPage();

    expect(cardIds()).toEqual(["aardvark-title", "mango-title", "zebra-title"]);
  });

  it("orders by human collation, not by raw code-unit order", async () => {
    // A lower-case initial sorts BETWEEN two upper-case ones for a reader
    // scanning an "A→Z" grid, but AFTER both under a raw code-unit comparator
    // ('b' = 0x62 > 'C' = 0x43). spec: FRONTEND_GOVERNANCE.md §Dashboard — the
    // sort is presented as `Title A→Z`, i.e. the alphabet the reader knows.
    const upperAlpha: MetricDefinitionListItem = {
      ...ALPHA,
      id: "upper-alpha-title",
      title: "Alpha ingest freshness",
      description: "Mike ingest freshness",
    };
    const lowerBravado: MetricDefinitionListItem = {
      ...BRAVO,
      id: "lower-bravado-title",
      title: "bravado validation coverage",
      description: "Zulu validation coverage",
    };
    const upperCharlie: MetricDefinitionListItem = {
      ...CHARLIE,
      id: "upper-charlie-title",
      title: "Charlie documentation completeness",
      description: "Tango documentation completeness",
    };
    setEnabledMetrics([upperCharlie, lowerBravado, upperAlpha]);
    await renderPage();

    expect(cardIds()).toEqual([
      "upper-alpha-title",
      "lower-bravado-title",
      "upper-charlie-title",
    ]);
  });
});

// ── Cap disclosure ─────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — cap disclosure", () => {
  it("discloses the shown count and the total when total_count exceeds the returned rows", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE], 7);
    await renderPage();

    // spec: the note "states that only the first 100 enabled metrics are shown"
    // — i.e. the returned row count — "and that the filter and sort apply to
    // those 100 only". Both numbers must appear alongside the spec's phrase; the
    // connective wording between them is the impl's to choose.
    expect(pageText()).toMatch(/\b3\b[\s\S]*\b7\b[\s\S]*enabled metrics/);
    expect(pageText()).toMatch(/filter and sort/i);
  });

  it("omits the disclosure when the read returned the whole catalogue", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE], 3);
    await renderPage();

    // Negated with the same whole-document matcher the positive case uses.
    // queryByText only sees an element's direct text nodes, so it would go
    // vacuous on exactly the re-wordings the positive assertion tolerates.
    expect(pageText()).not.toMatch(/enabled metrics/);
    // Backstop: the page really did render (the assertion above is not vacuous).
    expect(cardIds()).toHaveLength(3);
  });

  it("keeps disclosing the cap while the controls narrow the shown set", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE], 7);
    await renderPage();

    await click(screen.getByRole("checkbox", { name: "validation-score" }));

    // The note describes the READ (3 of 7 fetched), not the post-filter count,
    // since it exists to explain what the controls could not reach.
    expect(cardIds()).toHaveLength(2);
    expect(pageText()).toMatch(/\b3\b[\s\S]*\b7\b[\s\S]*enabled metrics/);
  });
});

// ── Empty states ───────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — the two empty states", () => {
  it("points at the Metrics page when nothing is enabled at all", async () => {
    setEnabledMetrics([]);
    await renderPage();

    // spec: "With no enabled metrics at all it points at the Metrics page as the
    // place to enable one."
    expect(screen.getByText(/Metrics page/i)).toBeInTheDocument();
  });

  it("points at the view controls (not the Metrics page) when the controls empty a non-empty set", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    // "Xray" opens BRAVO's `description` and appears in no title: a search that
    // read `description` would keep a card and never reach this state.
    await type("Search titles", "Xray");

    // spec: "With enabled metrics present but none surviving the type filter and
    // title search it points at the view controls instead."
    expect(cardIds()).toEqual([]);
    expect(screen.queryByText(/Metrics page/i)).not.toBeInTheDocument();
    expect(screen.getByText(/controls/i)).toBeInTheDocument();
  });

  it("returns to the cards once the controls are relaxed again", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    await type("Search titles", "zzz-nothing-matches");
    expect(cardIds()).toEqual([]);

    await type("Search titles", "");
    expect(cardIds()).toHaveLength(3);
  });
});

// ── Before the read resolves ───────────────────────────────────────────────────
//
// Nothing the page derives from the envelope (the view-filtered grid, the cap
// note) may be computed before there is an envelope. These two states are the
// FIRST paint of every dashboard load, so a page that only ever sees a resolved
// read is untested where it is most exercised.

describe("GovernanceDashboardPage — the read is in flight or failed", () => {
  it("shows placeholders and no grid content while the enabled-metrics read is loading", async () => {
    mockUseEnabledMetrics.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    await renderPage();

    // The controls mount regardless — they are client-side over whatever arrives.
    expect(screen.getByLabelText("Search titles")).toBeInTheDocument();

    // Nothing derived from the (absent) envelope renders: no card, no cap note,
    // and neither empty state — spec: the two empty states describe a resolved
    // read ("no enabled metrics at all" / "none surviving the … search"), and an
    // unresolved one is neither.
    expect(cardIds()).toEqual([]);
    expect(screen.queryByText(/enabled metrics/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Metrics page/i)).not.toBeInTheDocument();

    // Loading placeholders stand in for the grid — spec:
    // FRONTEND_BASIC.md §Shared Component Notes, the loading-affordance
    // convention (an in-flight read shows a placeholder in the grid's place).
    // `animate-pulse` is the shared Skeleton component's marker class, chosen as
    // the observable proxy; the spec names the affordance, not the class.
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("surfaces a failed enabled-metrics read as an error state, with no grid content", async () => {
    mockUseEnabledMetrics.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    });
    await renderPage();

    // spec: FRONTEND_BASIC.md §Query Error Policy — a failure is "surfaced to the
    // render site"; the message names what the read was for.
    expect(screen.getByText(/failed to load metrics/i)).toBeInTheDocument();
    expect(screen.getByText(/boom/i)).toBeInTheDocument();

    // A failed read is not an empty catalogue: neither empty state may appear,
    // and no card or cap note may be computed from the missing envelope.
    expect(cardIds()).toEqual([]);
    expect(screen.queryByText(/Metrics page/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/of \d+ enabled metrics/i)).not.toBeInTheDocument();
  });
});

// ── The controls are client-side ───────────────────────────────────────────────

describe("GovernanceDashboardPage — the view controls add no request parameter", () => {
  it("issues the same enabled-metrics read no matter how the controls are set", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    const readAtDefaults = mockUseEnabledMetrics.mock.calls.at(-1);

    await click(screen.getByRole("checkbox", { name: "doc-health" }));
    await type("Search titles", "romeo");
    await click(descOption());

    // Backstop: the three controls really did reshape the grid, so the equality
    // below reads as "view changed, read didn't" — not "the controls were inert".
    expect(cardIds()).toEqual(["bravo-validation"]);

    // spec: the controls run over "the same GET /spoke/governance/metric (filter
    // is_enabled=true) read that backs the cards — no request parameter".
    // The FULL argument list is compared, not merely the absence of a filter
    // key: the leak the spec forbids is any changed parameter.
    expect(mockUseEnabledMetrics.mock.calls.at(-1)).toEqual(readAtDefaults);
    expect(readAtDefaults).toEqual([]);
  });
});

// ── Persistence ────────────────────────────────────────────────────────────────

describe("GovernanceDashboardPage — the view persists across visits", () => {
  it("restores the type / search / sort selection on a later mount", async () => {
    setEnabledMetrics([ALPHA, BRAVO, CHARLIE]);
    await renderPage();

    await click(screen.getByRole("checkbox", { name: "ingestion-freshness" }));
    // "Tango" sits mid-title in BRAVO and CHARLIE only and in no description,
    // so the persisted search has to be doing real work for the pair below.
    await type("Search titles", "Tango");
    await click(descOption());
    expect(cardIds()).toEqual(["charlie-doc-health", "bravo-validation"]);

    // A fresh mount stands in for the next visit to the dashboard.
    cleanup();
    await renderPage();

    // spec: "Each selection persists across visits in browser `localStorage`
    // under a stable key".
    expect(screen.getByRole("checkbox", { name: "ingestion-freshness" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.getByLabelText("Search titles")).toHaveValue("Tango");
    // The sort control itself is restored too, not just the resulting order.
    expect(screen.getByTestId("sort-select-root")).toHaveAttribute(
      "data-value",
      descOption().getAttribute("data-value"),
    );
    expect(cardIds()).toEqual(["charlie-doc-health", "bravo-validation"]);
    expect(localStorage.getItem(METRIC_VIEW_KEYS.governanceDashboard)).toBeTruthy();
  });
});
