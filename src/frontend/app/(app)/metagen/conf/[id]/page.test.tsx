/**
 * Tests for the metagen conf detail page.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Conf create / detail.
 *   - Editor sees Edit, Run, Delete; Reader sees none.
 *   - The conditional Edit/Cancel header button must NOT submit the form on the
 *     first Edit click. This guards the React-node-reuse morph bug
 *     (memory project_frontend_button_submit_morph): conditional buttons in the
 *     same slot need distinct keys. jsdom can't catch the real submit-on-morph,
 *     but it CAN verify that clicking Edit fires no PUT mutation.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import React from "react";
import MetagenConfDetailPage from "./page";
import type { MetagenConf } from "@/types/metagen";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mockConf = vi.fn();
const putMutate = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenConf: () => mockConf(),
  useUpdateMetagenConf: () => ({
    put: { mutate: putMutate, isPending: false, error: null },
    patch: { mutate: vi.fn(), isPending: false, error: null },
  }),
  useDeleteMetagenConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRunMetagenConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useMetagenConfEvents: () => ({ data: { events: [], total_count: 0 } }),
  useMetagenCoveredDatasets: () => ({
    data: { datasets: [], total_count: 0 },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// RangePicker pulls in calendar internals not needed here.
vi.mock("@/components/range-picker", () => ({
  RangePicker: () => React.createElement("div", { "data-testid": "range-picker" }),
}));

// MetagenConfForm pulls in Radix Select / DatasetFilterEditor (ResizeObserver,
// not in jsdom). The detail page exercises header gating + the Edit→Save morph,
// not the form internals, so stub it: it renders the Save button only when not
// disabled (edit mode on), mirroring the real component's contract.
vi.mock("@/components/metagen/conf-form", () => ({
  MetagenConfForm: ({
    disabled,
    submitLabel,
  }: {
    disabled?: boolean;
    submitLabel?: string;
  }) =>
    React.createElement(
      "form",
      { "data-testid": "conf-form" },
      disabled
        ? null
        : React.createElement(
            "button",
            { type: "submit" },
            submitLabel ?? "Save configuration",
          ),
    ),
}));

vi.mock("@/components/metagen/metagen-event-table", () => ({
  MetagenEventTable: () => React.createElement("div", { "data-testid": "event-table" }),
}));

vi.mock("@/components/metagen/covered-table", () => ({
  MetagenCoveredTable: () => React.createElement("div", { "data-testid": "covered-table" }),
}));

vi.mock("@/components/metagen/run-dialog", () => ({
  RunDialog: () => null,
}));

function makeConf(): MetagenConf {
  return {
    id: "conf-1",
    name: "catalog policy",
    is_enabled: true,
    schedule_tier: "daily",
    dataset_filter: {},
    result_limit: 3,
    overwrite_pending: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
  };
}

async function renderPage() {
  const params = Promise.resolve({ id: "conf-1" });
  await act(async () => {
    render(
      <React.Suspense fallback={<div data-testid="suspense-fallback" />}>
        <MetagenConfDetailPage params={params} />
      </React.Suspense>,
    );
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockConf.mockReset();
  putMutate.mockReset();
});

describe("metagen conf detail — write gating", () => {
  it("Editor sees Edit, Run, and Delete controls", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    expect(screen.getByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^run$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
  });

  it("Reader sees no write controls", async () => {
    mockUseMe.mockReturnValue({ canWrite: false });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
  });

  it("clicking Edit does NOT submit the form (no PUT mutation fires)", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    const editButton = screen.getByRole("button", { name: /^edit$/i });
    await act(async () => {
      fireEvent.click(editButton);
    });

    // Edit only toggles edit mode; it must not trigger the conf PUT mutation.
    expect(putMutate).not.toHaveBeenCalled();
    // After clicking Edit, the form's Save button appears (edit mode is on).
    expect(screen.getByRole("button", { name: /save conf/i })).toBeTruthy();
    // Cancel replaces Edit (distinct keys, no node reuse).
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
  });
});
