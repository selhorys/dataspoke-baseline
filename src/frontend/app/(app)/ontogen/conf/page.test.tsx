/**
 * Tests for app/(app)/ontogen/conf/page.tsx — OntoGen Configuration page.
 *
 * The header renders `editing ? (Save, Cancel) : (Edit, Run)` in the SAME
 * conditional slot. In a REAL browser, the reported UC3 defect was: clicking
 * "Edit" fired PUT /spoke/ontogen/attr/conf + a "Configuration saved" toast and
 * never entered edit mode. Mechanism: React reused the same <button> DOM node
 * across the ternary and morphed it into the `type="submit" form="ontogen-conf-form"`
 * Save button DURING the click handler's setEditing flush, so the browser
 * performed the click's default submit action on the now-submit node. `type="button"`
 * on Edit does not help because the very node is reused as Save.
 *
 * The fix: distinct `key` props on the four conditional header buttons
 * (`conf-save` / `conf-cancel` / `conf-edit` / `conf-run`) so React creates a
 * separate DOM node per branch instead of reusing/morphing one. `type="button"`
 * is kept on Edit/Cancel/Run as defensive hardening.
 *
 * IMPORTANT — jsdom limitation: jsdom does NOT model the browser default-action
 * phase, so it cannot reproduce the morph-then-submit behavior. The behavioral
 * "Edit must not submit" subtests below PASS even on the unfixed code; they are
 * therefore NOT the real regression guard. The genuine behavioral guard is the
 * Playwright spec
 *   tests/e2e/ground/ontogen/conf-edit-no-submit.spec.ts
 * which asserts in a real browser that clicking Edit fires NO PUT request and
 * enters edit mode. As a unit-layer structural guard, `structurally — distinct
 * key props on the conditional header buttons` reads the page source and asserts
 * the four distinct keys are present (their absence is exactly what reintroduces
 * the defect).
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md §Configuration (attr/conf):
 *       Edit toggles an editable form; Save submits PUT /spoke/ontogen/attr/conf;
 *       Cancel discards edits without writing.
 *   - lib/api/ontogen.ts useUpsertOntogenConf → PUT /spoke/ontogen/attr/conf.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ApiError } from "@/lib/api/client";
import type { OntogenConf } from "@/types/ontogen";

// ---------------------------------------------------------------------------
// Browser API stubs — jsdom lacks ResizeObserver (used by Radix UI Select)
// ---------------------------------------------------------------------------
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ---------------------------------------------------------------------------
// Mock factory — the default conf the API returns even when none was created.
// ---------------------------------------------------------------------------
function makeConf(overrides: Partial<OntogenConf> = {}): OntogenConf {
  return {
    is_enabled: false,
    schedule_tier: null,
    dataset_filter: "",
    default_run_prompt: null,
    updated_at: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

// useMe — canWrite:true so the header action buttons render
const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

// ontogen API hooks — controllable per-test
const mockUseOntogenConf = vi.fn();
const mockUpsertMutate = vi.fn();
const mockRunMutate = vi.fn();
/** Mutable so a test can put a server error on the upsert (Save) mutation. */
let upsertError: unknown = null;
vi.mock("@/lib/api/ontogen", () => ({
  useOntogenConf: () => mockUseOntogenConf(),
  useUpsertOntogenConf: () => ({
    mutate: mockUpsertMutate,
    isPending: false,
    error: upsertError,
  }),
  useRunOntogen: () => ({ mutate: mockRunMutate, isPending: false }),
}));

// toast — capture calls
const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// ---------------------------------------------------------------------------
// Import the page AFTER mocks are registered
// ---------------------------------------------------------------------------
import OntogenConfPage from "./page";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function editorMe() {
  return {
    me: {
      id: "u1",
      email: "editor@example.com",
      name: "Editor",
      role: "Editor" as const,
      has_password: true,
      has_google: false,
      created_at: "",
      updated_at: "",
    },
    isAdmin: false,
    isEditor: true,
    canWrite: true,
    isLoading: false,
  };
}

/** The conf form's is_enabled checkbox — a stable disabled/enabled probe. */
function isEnabledCheckbox(): HTMLElement {
  const el = document.getElementById("conf-is-enabled");
  if (!el) throw new Error("conf-is-enabled checkbox not found");
  return el;
}

function checkboxIsDisabled(el: HTMLElement): boolean {
  // Radix Checkbox renders a <button> with `disabled` attribute + data-disabled.
  return (
    (el as HTMLButtonElement).disabled === true ||
    el.getAttribute("data-disabled") !== null ||
    el.getAttribute("aria-disabled") === "true"
  );
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.clearAllMocks();
  cleanup();
  upsertError = null;
  mockUseMeFn.mockReturnValue(editorMe());
  mockUseOntogenConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
});

// ---------------------------------------------------------------------------
// 1. Initial (view) state — form disabled, Edit shown, no mutation
// ---------------------------------------------------------------------------
describe("OntogenConfPage — initial view state (FRONTEND_ONTOGEN.md §Configuration)", () => {
  it("renders the read-only view (no form inputs), shows Edit, and does NOT call the upsert mutation", () => {
    render(<OntogenConfPage />);

    // View mode renders plain text, not the editable form — no conf inputs present.
    expect(document.getElementById("conf-is-enabled")).toBeNull();
    // is_enabled is shown as plain text (default conf has is_enabled:false).
    expect(screen.getByText("disabled")).toBeInTheDocument();

    // An Edit button is shown; Save is not.
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();

    // No write has happened just from rendering.
    expect(mockUpsertMutate).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 2. Clicking Edit enters edit mode WITHOUT submitting (the reported defect)
//
// NOTE: jsdom cannot reproduce the real-browser morph-then-submit (no default-
// action phase), so these "no mutation on Edit" assertions pass even on the
// unfixed code. They verify the happy-path state transition, not the defect.
// The real regression guard is the Playwright spec referenced in the file header
// plus the structural key assertion below.
// ---------------------------------------------------------------------------
describe("OntogenConfPage — Edit enters edit mode without submitting (UC3 defect)", () => {
  it("clicking Edit reveals the editable form and Save, and does NOT fire the upsert mutation", async () => {
    const user = userEvent.setup();
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));

    // Edit mode: the form inputs appear (enabled), Save visible, Edit gone.
    await waitFor(() => {
      expect(document.getElementById("conf-is-enabled")).not.toBeNull();
    });
    expect(checkboxIsDisabled(isEnabledCheckbox())).toBe(false);
    expect(screen.getByRole("button", { name: /^save$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();

    // The crux: clicking Edit must NOT have submitted the form.
    expect(mockUpsertMutate).not.toHaveBeenCalled();
    // And no "Configuration saved" toast should have fired.
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("the Edit button is an explicit type=button (not an implicit submit)", () => {
    // Defensive-hardening guard: Edit carries type="button" so it never defaults
    // to type="submit". This alone does NOT prevent the real defect (the node is
    // reused as the type="submit" Save button), but it is kept as belt-and-braces.
    render(<OntogenConfPage />);
    const editBtn = screen.getByRole("button", { name: /^edit$/i }) as HTMLButtonElement;
    expect(editBtn.type).toBe("button");
  });

  it("structurally — the conditional header buttons carry distinct key props", () => {
    // The ACTUAL fix: distinct `key` props on the four conditional header buttons
    // force React to create a separate DOM node per ternary branch instead of
    // reusing/morphing one <button> into the type="submit" Save node during the
    // setEditing flush (the real-browser submit-on-Edit defect). `key` is a
    // React-internal prop, not rendered to the DOM, so it cannot be asserted via
    // the DOM tree — assert its presence in the page source instead. Removing any
    // of these keys is exactly what reintroduces the defect.
    const here = dirname(fileURLToPath(import.meta.url));
    const src = readFileSync(join(here, "page.tsx"), "utf-8");
    for (const key of [
      'key="conf-save"',
      'key="conf-cancel"',
      'key="conf-edit"',
      'key="conf-run"',
    ]) {
      expect(src, `page.tsx must keep ${key} on the conditional header button`).toContain(key);
    }
  });
});

// ---------------------------------------------------------------------------
// 3. Save submits the upsert mutation exactly once
// ---------------------------------------------------------------------------
describe("OntogenConfPage — Save submits PUT /spoke/ontogen/attr/conf", () => {
  it("clicking Save after Edit fires the upsert mutation exactly once", async () => {
    const user = userEvent.setup();
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^save$/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalledTimes(1);
    });

    // The submitted body matches the default conf put-body shape.
    const body = mockUpsertMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(body).toHaveProperty("is_enabled", false);
    expect(body).toHaveProperty("schedule_tier", null);
    expect(body).toHaveProperty("dataset_filter");
  });
});

// ---------------------------------------------------------------------------
// 4. Cancel exits edit mode without submitting
// ---------------------------------------------------------------------------
describe("OntogenConfPage — Cancel discards edits without writing", () => {
  it("clicking Cancel returns to view mode and does NOT fire the upsert mutation", async () => {
    const user = userEvent.setup();
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Back to view mode: Edit shown, the editable form inputs gone.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^edit$/i })).toBeInTheDocument();
    });
    expect(document.getElementById("conf-is-enabled")).toBeNull();

    // Cancel never writes.
    expect(mockUpsertMutate).not.toHaveBeenCalled();
  });

  it("the Cancel button is an explicit type=button (not an implicit submit)", async () => {
    const user = userEvent.setup();
    render(<OntogenConfPage />);
    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    const cancelBtn = (await screen.findByRole("button", {
      name: /^cancel$/i,
    })) as HTMLButtonElement;
    expect(cancelBtn.type).toBe("button");
  });
});

// ---------------------------------------------------------------------------
// 5. Run button does not submit the conf form
// ---------------------------------------------------------------------------
describe("OntogenConfPage — Run opens the run dialog without submitting the conf form", () => {
  it("clicking Run does NOT fire the upsert mutation", async () => {
    const user = userEvent.setup();
    render(<OntogenConfPage />);

    const runButton = screen.getByRole("button", { name: /^run$/i });
    await user.click(runButton);

    // The run dialog opening must not have submitted the conf form.
    expect(mockUpsertMutate).not.toHaveBeenCalled();
    // And it must not have fired a "Configuration saved" toast.
    expect(mockToast).not.toHaveBeenCalled();
    // The run dialog is open (a "Run ontology inference" title appears).
    await waitFor(() => {
      const dialogs = screen.getAllByText(/run ontology inference/i);
      expect(within(dialogs[0].closest("body") ?? document.body).getAllByText(/run ontology inference/i).length).toBeGreaterThan(0);
    });
  });

  it("the Run button is an explicit type=button (not an implicit submit)", () => {
    render(<OntogenConfPage />);
    const runBtn = screen.getByRole("button", { name: /^run$/i }) as HTMLButtonElement;
    expect(runBtn.type).toBe("button");
  });
});

// ---------------------------------------------------------------------------
// 6. dataset_filter is the shared SQL clause editor
//
// spec: spec/feature/FRONTEND_ONTOGEN.md §Configuration — "`dataset_filter` is a
//   SQL `WHERE`-clause string, edited through the shared DatasetFilterEditor".
// spec: spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor —
//   "Validation is server-side: a `422 INVALID_DATASET_FILTER` renders inline
//   against the field, carrying the position the API reported."
// spec: spec/API.md §`dataset_filter` grammar — the clause is one string; the
//   empty string matches every registered dataset.
// ---------------------------------------------------------------------------

/** The editor's fieldset — the inline slot a filter 422 must land in. */
function filterFieldset(): HTMLElement {
  const box = screen.getByLabelText("dataset_filter");
  const set = box.closest("fieldset");
  if (!set) throw new Error("dataset_filter editor is not inside a fieldset");
  return set as HTMLElement;
}

describe("OntogenConfPage — dataset_filter is a SQL clause string", () => {
  it("submits the clause the user typed, verbatim, as a string", async () => {
    const user = userEvent.setup();
    mockUseOntogenConf.mockReturnValue({
      data: makeConf({ dataset_filter: "origin = 'DEV'" }),
      isLoading: false,
      error: null,
    });
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    const box = (await screen.findByLabelText("dataset_filter")) as HTMLTextAreaElement;
    expect(box.value).toBe("origin = 'DEV'");

    // The UC3 Imazon clause — a tag-membership predicate.
    const clause = "'urn:li:tag:area:catalog' IN tag_urns";
    fireEvent.change(box, { target: { value: clause } });
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(mockUpsertMutate).toHaveBeenCalledTimes(1));
    const body = mockUpsertMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(body["dataset_filter"]).toBe(clause);
  });

  it("renders a 422 INVALID_DATASET_FILTER inline in the editor, with its position", async () => {
    const user = userEvent.setup();
    upsertError = new ApiError(
      {
        error_code: "INVALID_DATASET_FILTER",
        message: "unknown column 'owner'",
        trace_id: "t-1",
        resp_time: "2026-01-01T00:00:00Z",
        detail: { position: 0 },
      },
      422,
    );
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));

    await screen.findByLabelText("dataset_filter");
    const alert = within(filterFieldset()).getByRole("alert");
    expect(alert.textContent).toContain("unknown column 'owner'");
    expect(alert.textContent).toContain("0");
  });

  it("leaves every other save error out of the editor's inline slot", async () => {
    // Backstop for the assertion above: the inline slot is reserved for filter
    // errors; a non-filter failure is surfaced by the page's own onError toast.
    const user = userEvent.setup();
    upsertError = new ApiError(
      {
        error_code: "CONFLICT",
        message: "conf changed underneath",
        trace_id: "t-2",
        resp_time: "2026-01-01T00:00:00Z",
      },
      409,
    );
    render(<OntogenConfPage />);

    await user.click(screen.getByRole("button", { name: /^edit$/i }));
    await screen.findByLabelText("dataset_filter");

    expect(within(filterFieldset()).queryByRole("alert")).toBeNull();
  });
});
