/**
 * Tests for RunDialog — the MetaGen run trigger dialog's dataset_urns override.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_METAGEN.md §Components → RunDialog: "Its optional
 *     `dataset_urns` override is a newline-separated textarea parsed on submit,
 *     following the same input contract as DatasetFilterEditor" — which owns the
 *     one-URN-per-line, edge-trim, blank-lines-dropped, commas-are-not-separators
 *     rules (spec/feature/FRONTEND_BASIC.md §Shared component notes).
 *   - spec/API.md §Metadata Generation → `POST /spoke/metagen/conf/{conf_id}/method/run`:
 *     "Optional body `{"dataset_urns": [...]}` narrows scope; `?dry_run=true`
 *     evaluates without persisting."
 *   - spec/API.md §Error codes → INVALID_DATASET_URN: a `dataset_urns` entry must be
 *     a well-formed `urn:li:dataset:(…)` URN — a comma-split fragment of one is not.
 *
 * Mocked: nothing but the browser APIs jsdom lacks; onRun is a spy standing in for
 * the page's run mutation. Vitest unit tier.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RunDialog } from "./run-dialog";
import type { MetagenRunBody } from "@/types/metagen";

// Radix Dialog/Checkbox rely on ResizeObserver and pointer capture; jsdom has neither.
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

const DATASET_URN_A =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
const DATASET_URN_B =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)";

function renderDialog() {
  const onRun = vi.fn<(body: MetagenRunBody) => void>();
  render(<RunDialog open onOpenChange={vi.fn()} onRun={onRun} isRunning={false} />);
  const urnsBox = screen.getByLabelText("dataset_urns (optional)") as HTMLTextAreaElement;
  return { onRun, urnsBox };
}

describe("RunDialog — dataset_urns override is newline-separated", () => {
  it("submits a pasted dataset URN as exactly one entry, structural commas intact", () => {
    const { onRun, urnsBox } = renderDialog();

    fireEvent.change(urnsBox, { target: { value: DATASET_URN_A } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(onRun).toHaveBeenCalledTimes(1);
    const body = onRun.mock.calls[0][0];
    expect(body.dataset_urns).toEqual([DATASET_URN_A]);
    expect(body.dataset_urns).toHaveLength(1);
  });

  it("submits two URNs on two lines as two entries", () => {
    const { onRun, urnsBox } = renderDialog();

    fireEvent.change(urnsBox, { target: { value: `${DATASET_URN_A}\n${DATASET_URN_B}` } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(onRun.mock.calls[0][0].dataset_urns).toEqual([DATASET_URN_A, DATASET_URN_B]);
  });

  it("edge-trims each line and drops blank lines", () => {
    const { onRun, urnsBox } = renderDialog();

    fireEvent.change(urnsBox, {
      target: { value: `  ${DATASET_URN_A}  \n\n\t${DATASET_URN_B}\n` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(onRun.mock.calls[0][0].dataset_urns).toEqual([DATASET_URN_A, DATASET_URN_B]);
  });

  it("keeps the raw text in the box while it is being typed", () => {
    const { urnsBox } = renderDialog();

    fireEvent.change(urnsBox, { target: { value: `${DATASET_URN_A}\n` } });

    expect(urnsBox.value).toBe(`${DATASET_URN_A}\n`);
  });

  it("sends a null dataset_urns when the box is left empty (all in-scope datasets)", () => {
    const { onRun } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(onRun.mock.calls[0][0].dataset_urns).toBeNull();
  });

  it("tells the user one URN per line, not comma-separated values", () => {
    // The hint is the only place the newline-only rule reaches the user; steering
    // someone back to a comma-joined list yields fragments the API rejects with
    // 422 INVALID_DATASET_URN.
    const { urnsBox } = renderDialog();

    const paragraphs = Array.from((urnsBox.parentElement as HTMLElement).querySelectorAll("p"));
    expect(paragraphs.length).toBeGreaterThan(0); // backstop: a hint is rendered at all
    const hint = paragraphs.map((p) => p.textContent ?? "").join(" ");

    expect(hint).toMatch(/one URN per line/i);
    expect(hint).not.toMatch(/comma[- ]separated/i);
  });

  it("sends a null dataset_urns when the box holds only whitespace", () => {
    const { onRun, urnsBox } = renderDialog();

    fireEvent.change(urnsBox, { target: { value: "  \n \n" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(onRun.mock.calls[0][0].dataset_urns).toBeNull();
  });
});

describe("RunDialog — dry_run toggle", () => {
  it("submits dry_run false by default and true once the toggle is checked", () => {
    const { onRun, urnsBox } = renderDialog();

    fireEvent.change(urnsBox, { target: { value: DATASET_URN_A } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(onRun.mock.calls[0][0].dry_run).toBe(false);

    fireEvent.click(screen.getByLabelText(/dry_run/));
    fireEvent.click(screen.getByRole("button", { name: "Dry Run" }));

    expect(onRun).toHaveBeenCalledTimes(2);
    const body = onRun.mock.calls[1][0];
    expect(body.dry_run).toBe(true);
    // The URN override survives the toggle.
    expect(body.dataset_urns).toEqual([DATASET_URN_A]);
  });
});
