/**
 * Tests for MetricForm — the metric create/edit form's series control and its
 * `dataset_filter` editor.
 *
 * Spec traces (spec/feature/FRONTEND_GOVERNANCE.md §Metric detail / form):
 *   - "The form's `metrics` control is **one row per emitted key** of the
 *     selected `metric_type` …: a checkbox selecting the key, a color control
 *     (native color swatch paired with a `#RRGGBB` text input, kept in sync) and
 *     an order number. Only checked rows are submitted, as `{name, color, idx}`.
 *     Duplicate `idx` values and a malformed hex color are surfaced inline before
 *     submit … Changing `metric_type` reseeds the rows to the new type's keys."
 *   - "`dataset_filter` is a SQL `WHERE`-clause string …, rendered through
 *     DatasetFilterView and edited through DatasetFilterEditor … A `422
 *     INVALID_DATASET_FILTER` from Save renders inline against the field."
 *   - spec/API.md §Metric — Definition body: `metrics` names are emitted keys of
 *     the type, `color` is `#RRGGBB`, `idx` a positive integer, both unique
 *     within the metric.
 *   - spec/USE_CASE_en.md §UC5 — the emitted keys per built-in metric type.
 *
 * Mocked: nothing but browser APIs jsdom lacks (ResizeObserver / pointer capture
 * for the Radix Selects). The Zod schema, react-hook-form wiring, and the shared
 * DatasetFilterEditor are exercised for real — the point of this file is the
 * gestures that reach them.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MetricForm } from "./metric-form";
import { DATASET_FILTER_MAX_CHARS } from "./metric-form.schema";
import type { MetricFormValues } from "@/types/governance";

// jsdom lacks the APIs the Radix Select trigger relies on.
if (typeof globalThis.ResizeObserver === "undefined") {
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

// ── Fixtures (inline, readable) ────────────────────────────────────────────────

const DOC_HEALTH_DEFAULTS: MetricFormValues = {
  mode: "active",
  metric_type: "doc-health",
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check across DEV datasets",
  metrics: [
    { name: "total", color: "#64748B", idx: 1 },
    { name: "doc_health", color: "#A855F7", idx: 2 },
  ],
  metric_conf: {},
  schedule_tier: "daily",
  is_enabled: true,
  dataset_filter: "origin = 'DEV'",
};

const onSubmit = vi.fn();

function renderForm(overrides: Partial<MetricFormValues> = {}) {
  onSubmit.mockReset();
  return render(
    <MetricForm
      defaultValues={{ ...DOC_HEALTH_DEFAULTS, ...overrides }}
      isCreate={false}
      onSubmit={onSubmit}
      isPending={false}
    />,
  );
}

/** The row of the metrics control for one emitted key. */
function seriesRow(key: string): HTMLElement {
  const checkbox = screen.getByRole("checkbox", { name: key });
  const row = checkbox.closest("div.flex") as HTMLElement | null;
  expect(row, `no series row rendered for ${key}`).not.toBeNull();
  return row as HTMLElement;
}

function colorSwatch(key: string): HTMLInputElement {
  return screen.getByLabelText(`${key} color swatch`) as HTMLInputElement;
}

function colorText(key: string): HTMLInputElement {
  return screen.getByLabelText(`${key} color`) as HTMLInputElement;
}

function orderInput(key: string): HTMLInputElement {
  return screen.getByLabelText(`${key} display order`) as HTMLInputElement;
}

async function save(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

beforeEach(() => {
  onSubmit.mockReset();
});

// ── One row per emitted key ────────────────────────────────────────────────────

describe("MetricForm — the metrics control is one row per emitted key", () => {
  it("renders a checkbox, a color swatch, a hex text input and an order number per key", () => {
    // spec/USE_CASE_en.md §UC5 — doc-health emits `total` and `doc_health`.
    renderForm();

    for (const key of ["total", "doc_health"]) {
      expect(screen.getByRole("checkbox", { name: key })).toBeInTheDocument();
      expect(within(seriesRow(key)).getByLabelText(`${key} color swatch`)).toBeInTheDocument();
      expect(within(seriesRow(key)).getByLabelText(`${key} color`)).toBeInTheDocument();
      expect(within(seriesRow(key)).getByLabelText(`${key} display order`)).toBeInTheDocument();
    }
  });

  it("renders no row for a key the selected type does not emit", () => {
    renderForm();
    expect(screen.queryByRole("checkbox", { name: "ingested_in_time" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "valid_confd" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "valid_in_time" })).toBeNull();
  });

  it("seeds each row from the metric's existing descriptor (checked, its color, its idx)", () => {
    renderForm();

    expect(screen.getByRole("checkbox", { name: "total" })).toBeChecked();
    expect(colorText("total").value).toBe("#64748B");
    expect(orderInput("total").value).toBe("1");

    expect(screen.getByRole("checkbox", { name: "doc_health" })).toBeChecked();
    expect(colorText("doc_health").value).toBe("#A855F7");
    expect(orderInput("doc_health").value).toBe("2");
  });

  it("reseeds the rows to the new type's keys when metric_type changes", () => {
    // spec: "Changing `metric_type` reseeds the rows to the new type's keys."
    renderForm();

    fireEvent.click(screen.getByRole("combobox", { name: /metric_type/i }));
    fireEvent.click(screen.getByRole("option", { name: "ingestion-freshness" }));

    // ingestion-freshness emits total + ingested_in_time (USE_CASE §UC5).
    expect(screen.getByRole("checkbox", { name: "ingested_in_time" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "doc_health" })).toBeNull();
    // A key both types emit keeps the color and order it already had.
    expect(colorText("total").value).toBe("#64748B");
    expect(orderInput("total").value).toBe("1");
  });
});

// ── Color control: swatch and hex text stay in sync ───────────────────────────

describe("MetricForm — the color swatch and its hex text are kept in sync", () => {
  it("moves the swatch when the hex text is edited", () => {
    renderForm();

    fireEvent.change(colorText("doc_health"), { target: { value: "#123456" } });

    expect(colorText("doc_health").value).toBe("#123456");
    expect(colorSwatch("doc_health").value.toUpperCase()).toBe("#123456");
  });

  it("writes the hex text when the swatch is moved", () => {
    renderForm();

    fireEvent.change(colorSwatch("total"), { target: { value: "#00ff00" } });

    expect(colorText("total").value.toUpperCase()).toBe("#00FF00");
  });

  it("leaves the swatch on a renderable color while a partial hex is being typed", () => {
    // The text box owns the value the user is typing; the native swatch cannot
    // display "#12" and must not overwrite what was typed.
    renderForm();

    fireEvent.change(colorText("total"), { target: { value: "#12" } });

    expect(colorText("total").value).toBe("#12");
    expect(colorSwatch("total").value).toMatch(/^#[0-9a-f]{6}$/i);
  });
});

// ── Only checked rows are submitted, as {name, color, idx} ────────────────────

describe("MetricForm — submission shape", () => {
  it("submits only the checked rows, as {name, color, idx} in idx order", async () => {
    renderForm();

    // Deselect `total`; keep doc_health, and give it order 1.
    fireEvent.click(screen.getByRole("checkbox", { name: "total" }));
    fireEvent.change(orderInput("doc_health"), { target: { value: "1" } });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].metrics).toEqual([
      { name: "doc_health", color: "#A855F7", idx: 1 },
    ]);
  });

  it("submits both rows in idx order when the orders are swapped", async () => {
    renderForm();

    fireEvent.change(orderInput("total"), { target: { value: "2" } });
    fireEvent.change(orderInput("doc_health"), { target: { value: "1" } });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].metrics).toEqual([
      { name: "doc_health", color: "#A855F7", idx: 1 },
      { name: "total", color: "#64748B", idx: 2 },
    ]);
  });

  it("submits the edited color with the descriptor", async () => {
    renderForm();

    fireEvent.change(colorText("doc_health"), { target: { value: "#123456" } });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].metrics).toContainEqual({
      name: "doc_health",
      color: "#123456",
      idx: 2,
    });
  });
});

// ── Inline validation before submit ───────────────────────────────────────────

describe("MetricForm — duplicate idx and malformed color are surfaced before submit", () => {
  it("blocks submit and surfaces a message when two checked rows share an idx", async () => {
    // spec/API.md §Metric — `idx` is "unique within the metric"; the form
    // surfaces it inline "before submit", so the write never leaves the browser.
    renderForm();

    fireEvent.change(orderInput("doc_health"), { target: { value: "1" } });
    await save();

    // Both colliding rows are flagged — the reader cannot tell which to change
    // from one message alone.
    await waitFor(() => expect(screen.getAllByText(/order must be unique/i)).toHaveLength(2));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("blocks submit and surfaces a message on a malformed hex color", async () => {
    renderForm();

    fireEvent.change(colorText("doc_health"), { target: { value: "#12345" } });
    await save();

    await waitFor(() =>
      expect(screen.getByText(/color must be a #rrggbb hex string/i)).toBeInTheDocument(),
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("blocks submit when no key is checked", async () => {
    renderForm();

    fireEvent.click(screen.getByRole("checkbox", { name: "total" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "doc_health" }));
    await save();

    await waitFor(() => expect(screen.getByText(/select at least one metric key/i)).toBeInTheDocument());
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits cleanly once the offending row is fixed", async () => {
    // Backstop for the three blocked-submit assertions: the Save button does
    // reach onSubmit when the same form is valid.
    renderForm();

    fireEvent.change(orderInput("doc_health"), { target: { value: "1" } });
    await save();
    await waitFor(() => expect(screen.getAllByText(/order must be unique/i)).not.toHaveLength(0));

    fireEvent.change(orderInput("doc_health"), { target: { value: "3" } });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  });
});

// ── dataset_filter is the shared SQL editor ───────────────────────────────────

describe("MetricForm — dataset_filter is the shared SQL clause editor", () => {
  it("seeds the editor with the stored clause and submits what the user typed", async () => {
    renderForm();

    const box = screen.getByLabelText("dataset_filter") as HTMLTextAreaElement;
    expect(box.value).toBe("origin = 'DEV'");

    fireEvent.change(box, {
      target: { value: "origin = 'PROD' AND 'urn:li:tag:area:catalog' IN tag_urns" },
    });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].dataset_filter).toBe(
      "origin = 'PROD' AND 'urn:li:tag:area:catalog' IN tag_urns",
    );
  });

  it("submits an empty clause as the empty string — the all-datasets filter", async () => {
    // spec/API.md §`dataset_filter` grammar: "empty string = all registered datasets".
    renderForm({ dataset_filter: "" });

    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].dataset_filter).toBe("");
  });

  it("renders a server-reported filter error inline against the field", () => {
    // spec: "A `422 INVALID_DATASET_FILTER` from Save renders inline against the
    // field" — the form's job is to route the error into the editor.
    render(
      <MetricForm
        defaultValues={DOC_HEALTH_DEFAULTS}
        isCreate={false}
        onSubmit={onSubmit}
        isPending={false}
        filterError={{ message: "INVALID_DATASET_FILTER: unexpected token", position: 14 }}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("INVALID_DATASET_FILTER");
    expect(alert.textContent).toContain("14");
  });

  it("does not validate the clause client-side — an unparseable clause still submits", async () => {
    // The backend owns the grammar (FRONTEND_BASIC.md §Shared component notes);
    // the form must not pre-empt it with a client-side grammar check.
    renderForm();

    fireEvent.change(screen.getByLabelText("dataset_filter"), {
      target: { value: "origin = = 'PROD' AND (" },
    });
    await save();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].dataset_filter).toBe("origin = = 'PROD' AND (");
  });

  it("surfaces the character cap inline instead of failing the submit silently", async () => {
    // spec/API.md §Payload caps: filter text ≤ 8000 chars. The field is driven by
    // setValue, so its schema error needs the editor's own slot — otherwise Save
    // is a no-op with nothing rendered.
    renderForm();

    fireEvent.change(screen.getByLabelText("dataset_filter"), {
      target: { value: `origin = '${"D".repeat(DATASET_FILTER_MAX_CHARS)}'` },
    });
    await save();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(String(DATASET_FILTER_MAX_CHARS));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
