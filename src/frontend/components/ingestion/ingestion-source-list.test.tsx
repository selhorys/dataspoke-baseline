/**
 * Tests for IngestionSourceList — read-only badge, filter-key select, empty state.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §List View:
 *     DATAHUB_MANAGED rows carry a read-only badge; ACTIVE_CUSTOM_MANAGED and PASSIVE do not.
 *   - spec/feature/FRONTEND_INGESTION.md §List View:
 *     the filter renders five options (ALL + two DataHub-managed regular/ad-hoc +
 *     Active + Passive) and fires onFilterKeyChange.
 *   - spec/feature/FRONTEND_INGESTION.md §List View: empty state when sources=[] and isLoading=false.
 *
 * Per-row count/status fan-out is covered by tests/lib/api/ingestion.test.ts
 * (useIngestionSourceDatasetCounts / useIngestionSourceLatestRuns).
 *
 * The component calls useIngestionSourceDatasetCounts and useIngestionSourceLatestRuns
 * internally. Both are mocked at the @/lib/api/ingestion module boundary.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { IngestionSourceList } from "./ingestion-source-list";
import type { IngestionSource } from "@/types/ingestion";

// ---------------------------------------------------------------------------
// Mock next/link, the hook module, and the Radix-based Select component.
//
// Radix UI Select uses a Portal + data-state="closed" by default in jsdom,
// so SelectItem labels are absent from the DOM until the user opens the
// dropdown. To avoid pointer-event interaction complexity in jsdom we replace
// the Select with a plain <select> that is fully queryable. This is the
// standard RTL approach for portalled dropdown components.
// ---------------------------------------------------------------------------

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// Replace the Radix Select with a plain native <select> so options are
// statically present in the DOM and fireEvent.change works without opening
// a portal popup.
vi.mock("@/components/ui/select", () => {
  return {
    Select: ({
      value,
      onValueChange,
      children,
    }: {
      value?: string;
      onValueChange?: (v: string) => void;
      children?: React.ReactNode;
    }) => {
      // Collect option values from context through a React context workaround:
      // We render children normally — Trigger is ignored, Content children
      // are mapped below. For simplicity, just pass through children and let
      // the test interact with the rendered SelectItems.
      return React.createElement(
        "div",
        { "data-testid": "select-root", "data-value": value },
        React.Children.map(children, (child: React.ReactNode) => {
          if (!React.isValidElement(child)) return child;
          return React.cloneElement(child as React.ReactElement<{
            onValueChange?: (v: string) => void;
          }>, { onValueChange });
        }),
      );
    },
    SelectTrigger: ({ children }: { children?: React.ReactNode }) =>
      React.createElement("div", { "data-testid": "select-trigger" }, children),
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
        { "data-testid": "select-content" },
        React.Children.map(children, (child: React.ReactNode) => {
          if (!React.isValidElement(child)) return child;
          return React.cloneElement(child as React.ReactElement<{
            onValueChange?: (v: string) => void;
          }>, { onValueChange });
        }),
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
          "data-testid": `select-item-${value}`,
          "data-value": value,
          type: "button",
          onClick: () => onValueChange?.(value),
        },
        children,
      ),
  };
});

// Mock the two hooks the component calls internally. Both return stable empty
// data so per-row count/status rendering is inert in these tests.
vi.mock("@/lib/api/ingestion", () => ({
  useIngestionSourceDatasetCounts: (_ids: string[]) =>
    (_ids ?? []).map(() => ({ data: undefined, isLoading: false })),
  useIngestionSourceLatestRuns: (_ids: string[]) =>
    (_ids ?? []).map(() => ({ data: undefined, isLoading: false })),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeSource(
  mode: IngestionSource["mode"],
  overrides: Partial<IngestionSource> = {},
): IngestionSource {
  return {
    id: `src-${mode}`,
    mode,
    name: `${mode} source`,
    schedule: mode === "ACTIVE_CUSTOM_MANAGED" ? "0 0 * * *" : null,
    recipe: { source: { type: "postgres", config: {} } },
    platform: "postgres",
    status: "OK",
    ad_hoc: false,
    datahub_source_urn: mode === "DATAHUB_MANAGED" ? "urn:li:dataHubIngestionSource:x" : null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-02T00:00:00Z",
    ...overrides,
  };
}

const basePage = { offset: 0, limit: 20, totalCount: 0 };
const noop = () => {};

// ---------------------------------------------------------------------------
// 1. Read-only badge — DATAHUB_MANAGED vs other modes
// ---------------------------------------------------------------------------

describe("IngestionSourceList — read-only badge", () => {
  it("renders a read-only badge for a DATAHUB_MANAGED row", () => {
    render(
      <IngestionSourceList
        sources={[makeSource("DATAHUB_MANAGED")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText("read-only")).toBeTruthy();
  });

  it("does NOT render a read-only badge for ACTIVE_CUSTOM_MANAGED rows", () => {
    render(
      <IngestionSourceList
        sources={[makeSource("ACTIVE_CUSTOM_MANAGED")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText("read-only")).toBeNull();
  });

  it("does NOT render a read-only badge for PASSIVE rows", () => {
    render(
      <IngestionSourceList
        sources={[makeSource("PASSIVE")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText("read-only")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Filter-key select — five options present + onFilterKeyChange callback
// ---------------------------------------------------------------------------

describe("IngestionSourceList — filter-key select", () => {
  it("renders all five filter option labels in the select content", () => {
    render(
      <IngestionSourceList
        sources={[]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // The Radix Select renders all SelectItem labels in the DOM (hidden via CSS).
    // DATAHUB_MANAGED is split into two disjoint regular/ad-hoc options.
    expect(screen.getByText("All")).toBeTruthy();
    expect(screen.getByText("DataHub-managed (regular)")).toBeTruthy();
    expect(screen.getByText("DataHub-managed (ad-hoc)")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("Passive")).toBeTruthy();
  });

  it("fires onFilterKeyChange with the selected filter key", () => {
    const onFilterKeyChange = vi.fn();
    render(
      <IngestionSourceList
        sources={[]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={onFilterKeyChange}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // Locate the Radix SelectItem by its text and fire a click to invoke onValueChange.
    const activeOption = screen.getByText("Active");
    fireEvent.click(activeOption);
    expect(onFilterKeyChange).toHaveBeenCalledWith("ACTIVE_CUSTOM_MANAGED");
  });
});

// ---------------------------------------------------------------------------
// 3. URN subtitle — DATAHUB_MANAGED shows datahub_source_urn; others do not
// ---------------------------------------------------------------------------

describe("IngestionSourceList — URN subtitle", () => {
  it("renders datahub_source_urn as a subtitle for a DATAHUB_MANAGED row", () => {
    // makeSource("DATAHUB_MANAGED") sets datahub_source_urn to
    // "urn:li:dataHubIngestionSource:x" — verify the text appears in the DOM.
    render(
      <IngestionSourceList
        sources={[makeSource("DATAHUB_MANAGED")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText("urn:li:dataHubIngestionSource:x")).toBeTruthy();
  });

  it("does NOT render a URN subtitle for an ACTIVE_CUSTOM_MANAGED row", () => {
    // makeSource("ACTIVE_CUSTOM_MANAGED") sets datahub_source_urn to null —
    // no urn:li:… text must appear in the DOM.
    render(
      <IngestionSourceList
        sources={[makeSource("ACTIVE_CUSTOM_MANAGED")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText(/^urn:/)).toBeNull();
  });

  it("does NOT render a URN subtitle for a PASSIVE row", () => {
    // makeSource("PASSIVE") sets datahub_source_urn to null.
    render(
      <IngestionSourceList
        sources={[makeSource("PASSIVE")]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText(/^urn:/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3b. Ad-hoc badge — present on ad_hoc:true rows, absent on ad_hoc:false rows
// ---------------------------------------------------------------------------
// Spec: spec/feature/FRONTEND_INGESTION.md §List View — an "ad-hoc" badge marks
// rows whose ad_hoc flag is true (CLI/Run-click sources synced as DATAHUB_MANAGED).

describe("IngestionSourceList — ad-hoc badge", () => {
  it("renders an 'ad-hoc' badge for an ad_hoc:true row", () => {
    render(
      <IngestionSourceList
        sources={[makeSource("DATAHUB_MANAGED", { ad_hoc: true })]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText("ad-hoc")).toBeTruthy();
  });

  it("does NOT render an 'ad-hoc' badge for an ad_hoc:false row", () => {
    render(
      <IngestionSourceList
        sources={[makeSource("DATAHUB_MANAGED", { ad_hoc: false })]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={{ ...basePage, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText("ad-hoc")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. Empty state — sources=[] and isLoading=false
// ---------------------------------------------------------------------------

describe("IngestionSourceList — empty state", () => {
  it("shows 'No ingestion sources found.' when sources=[] and isLoading=false", () => {
    render(
      <IngestionSourceList
        sources={[]}
        isLoading={false}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText(/no ingestion sources found/i)).toBeTruthy();
  });

  it("does NOT show the empty-state message when isLoading=true", () => {
    render(
      <IngestionSourceList
        sources={[]}
        isLoading={true}
        filterKey="ALL"
        onFilterKeyChange={noop}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByText(/no ingestion sources found/i)).toBeNull();
  });
});
