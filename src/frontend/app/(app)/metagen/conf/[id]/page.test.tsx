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
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import React from "react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import MetagenConfDetailPage from "./page";
import { ApiError } from "@/lib/api/client";
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
/** Mutable so a test can put a server error on the PUT mutation. */
let putError: unknown = null;
vi.mock("@/lib/api/metagen", () => ({
  useMetagenConf: () => mockConf(),
  useUpdateMetagenConf: () => ({
    put: { mutate: putMutate, isPending: false, error: putError },
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
// not the form internals, so stub it as a bare <form id={formId}> shell with NO
// internal submit button — mirroring the real component's contract (the Save
// button lives in the page header and is wired via form={formId} type="submit").
vi.mock("@/components/metagen/conf-form", () => ({
  MetagenConfForm: ({
    formId,
    serverError,
    datasetFilterError,
  }: {
    formId: string;
    serverError?: string;
    datasetFilterError?: { message: string };
  }) =>
    React.createElement("form", {
      id: formId,
      "data-testid": "conf-form",
      // The page decides which slot an error lands in; surface both to assert on.
      "data-server-error": serverError ?? "",
      "data-filter-error": datasetFilterError?.message ?? "",
    }),
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
    dataset_filter: "",
    result_limit: 3,
    overwrite_pending: true,
    dataset_affected_count: 0,
    last_run_at: null,
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

// Radix Dialog/portal internals depend on ResizeObserver, absent in jsdom.
beforeAll(() => {
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

beforeEach(() => {
  mockUseMe.mockReset();
  mockConf.mockReset();
  putMutate.mockReset();
  putError = null;
});

describe("metagen conf detail — read-only view vs edit form", () => {
  it("opens as a read-only view (plain text, no form) and swaps to the form on Edit", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    // View mode: the conf is rendered as plain text, not the editable form.
    expect(screen.queryByTestId("conf-form")).toBeNull();
    // is_enabled shows as text ("enabled" for the enabled conf).
    expect(screen.getAllByText("enabled").length).toBeGreaterThan(0);

    // Edit swaps the view for the form.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    });
    expect(screen.getByTestId("conf-form")).toBeInTheDocument();
  });

  it("Reader sees the read-only view and no form", async () => {
    mockUseMe.mockReturnValue({ canWrite: false });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.getAllByText("enabled").length).toBeGreaterThan(0);
  });
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

  it("clicking Edit switches to Save/Cancel and HIDES Run/Delete, without firing PUT", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    const editButton = screen.getByRole("button", { name: /^edit$/i });
    await act(async () => {
      fireEvent.click(editButton);
    });

    // Edit only toggles edit mode; it must not trigger the conf PUT mutation.
    // jsdom can't reproduce the real-browser morph-then-submit, so this guards
    // the state transition, not the defect (the e2e ground spec is the real guard).
    expect(putMutate).not.toHaveBeenCalled();

    // After clicking Edit, the header Save button appears (label "Save", not the
    // old internal "Save conf" label), bound to the conf form via form=.
    const saveButton = screen.getByRole("button", {
      name: /^save$/i,
    }) as HTMLButtonElement;
    expect(saveButton.type).toBe("submit");
    expect(saveButton.getAttribute("form")).toBe("metagen-conf-form");

    // Cancel replaces Edit (distinct keys, no node reuse).
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();

    // Run and Delete are hidden while editing.
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
  });

  it("clicking Cancel returns to read mode (Edit/Run/Delete restored)", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    });

    // Back to read mode: Edit/Run/Delete shown, Save/Cancel gone.
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^run$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).toBeNull();
    expect(putMutate).not.toHaveBeenCalled();
  });

  it("structurally — the conditional header buttons carry distinct key props", () => {
    // The ACTUAL morph fix: distinct `key` props on the conditional header buttons
    // force React to create a separate DOM node per ternary branch instead of
    // reusing/morphing one <button> into the type="submit" Save node during the
    // setEditing flush. `key` is a React-internal prop (not rendered to the DOM),
    // so assert its presence in the page source. Removing any reintroduces the bug.
    const here = dirname(fileURLToPath(import.meta.url));
    const src = readFileSync(join(here, "page.tsx"), "utf-8");
    for (const key of ['key="conf-save"', 'key="conf-cancel"', 'key="conf-edit"']) {
      expect(
        src,
        `page.tsx must keep ${key} on the conditional header button`,
      ).toContain(key);
    }
  });
});

describe("metagen conf detail — a save error lands in exactly one slot", () => {
  // spec/feature/FRONTEND_BASIC.md §Shared component notes: a 422
  // INVALID_DATASET_FILTER "renders inline against the field". The generic slot
  // is wired to the conf `name` Field, so routing the filter error there too
  // would mislabel a valid name as invalid.
  it("routes a 422 INVALID_DATASET_FILTER to the editor only", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    putError = new ApiError(
      {
        error_code: "INVALID_DATASET_FILTER",
        message: "unexpected token (at character 17)",
        trace_id: "t-1",
        resp_time: "2026-01-01T00:00:00Z",
        detail: { position: 17 },
      },
      422,
    );

    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    });

    const form = screen.getByTestId("conf-form");
    expect(form.getAttribute("data-filter-error")).toContain("INVALID_DATASET_FILTER");
    expect(form.getAttribute("data-server-error")).toBe("");
  });

  it("keeps every other save error in the generic slot", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    putError = new ApiError(
      {
        error_code: "CONFLICT",
        message: "name already used",
        trace_id: "t-2",
        resp_time: "2026-01-01T00:00:00Z",
      },
      409,
    );

    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    });

    const form = screen.getByTestId("conf-form");
    expect(form.getAttribute("data-server-error")).toContain("CONFLICT");
    expect(form.getAttribute("data-filter-error")).toBe("");
  });
});

describe("metagen conf-form — no internal submit element (header external-submit)", () => {
  it("conf-form.tsx renders no internal submit button", () => {
    // The Save/Create submit lives in the page header (external submit via
    // form={formId}); the form component itself must carry NO submit element,
    // mirroring OntoGen. Read the component source and assert it.
    const here = dirname(fileURLToPath(import.meta.url));
    // app/(app)/metagen/conf/[id] → repo src/frontend/components/metagen/conf-form.tsx
    const formPath = join(
      here,
      "..",
      "..",
      "..",
      "..",
      "..",
      "components",
      "metagen",
      "conf-form.tsx",
    );
    let formSrc = readFileSync(formPath, "utf-8");
    // Strip comments so a documented "submit" in prose doesn't false-RED.
    formSrc = formSrc
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/[^\n]*/g, "$1");

    // No <Button> may default to type="submit": any <Button> tag without an
    // explicit type="button" is a default-submit button (false-RED-safe: a
    // legitimate type="button" Button is allowed).
    const defaultSubmitButtons = (formSrc.match(/<Button\b(?:[^>]|=>)*?>/g) ?? []).filter(
      (tag) => !/type=["']button["']/.test(tag),
    );
    expect(defaultSubmitButtons).toEqual([]);

    // And no explicit type="submit" anywhere in the form component.
    expect(/type=["']submit["']/.test(formSrc)).toBe(false);
  });
});

describe("metagen conf detail — delete confirm copy", () => {
  it("the delete dialog states results are retained as parentless, not dropped", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });

    await renderPage();

    const deleteButton = screen.getByRole("button", { name: /^delete$/i });
    await act(async () => {
      fireEvent.click(deleteButton);
    });

    // The confirm dialog must communicate RETENTION, not deletion, of generated
    // items/candidates. Deleting a conf orphans them (parentless), it does not
    // drop them. spec: feature/BACKEND.md §Metadata Generation Service — retention.
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent ?? "").toMatch(/retained/i);
    expect(dialog.textContent ?? "").toMatch(/parentless/i);
    // Must NOT use deletion/drop language for the retained results.
    expect(dialog.textContent ?? "").not.toMatch(/dropped|deleted along|will be removed/i);
  });
});
