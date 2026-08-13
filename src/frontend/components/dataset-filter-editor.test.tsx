/**
 * Tests for DatasetFilterEditor — the `dataset_filter` SQL clause editor.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor:
 *     "One vertically resizable monospace textarea holding the clause verbatim…
 *     An Auto-indent button reformats the text in place… The formatter is purely
 *     lexical and holds no grammar knowledge… Validation is server-side: a 422
 *     INVALID_DATASET_FILTER renders inline against the field, carrying the
 *     position the API reported. A folded grammar guide sits beneath the box.
 *     The editor reseeds from props only when the incoming filter is not the one
 *     it last emitted."
 *   - spec/API.md §`dataset_filter` grammar.
 *
 * Mocked: nothing — the component is pure client state. Vitest unit tier.
 */
import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { DatasetFilterEditor } from "./dataset-filter-editor";

/** Mirrors the real parents (metric form via `watch`, conf pages via useState). */
function ControlledEditor({
  initial = "",
  onEmit,
  loaded,
  error,
}: {
  initial?: string;
  onEmit?: (v: string) => void;
  /** A record "loaded from the API" — applied by the Load record button. */
  loaded?: string;
  error?: { message: string; position?: number };
}) {
  const [filter, setFilter] = useState<string>(initial);
  return (
    <>
      <DatasetFilterEditor
        value={filter}
        error={error}
        onChange={(v) => {
          onEmit?.(v);
          setFilter(v);
        }}
      />
      {loaded !== undefined && (
        <button type="button" onClick={() => setFilter(loaded)}>
          Load record
        </button>
      )}
    </>
  );
}

function box(): HTMLTextAreaElement {
  return screen.getByLabelText("dataset_filter") as HTMLTextAreaElement;
}

function lastEmitted(onEmit: ReturnType<typeof vi.fn>): string {
  expect(onEmit).toHaveBeenCalled();
  return onEmit.mock.calls[onEmit.mock.calls.length - 1][0] as string;
}

describe("DatasetFilterEditor — one resizable monospace box holding the clause", () => {
  it("emits the raw text typed, verbatim", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(box(), { target: { value: "origin = 'PROD'  " } });

    expect(box().value).toBe("origin = 'PROD'  ");
    expect(lastEmitted(onEmit)).toBe("origin = 'PROD'  ");
  });

  it("is vertically resizable and monospace", () => {
    render(<ControlledEditor />);
    expect(box().classList.contains("resize-y")).toBe(true);
    expect(box().classList.contains("font-mono")).toBe(true);
  });

  it("reseeds from props only when a freshly loaded record differs", () => {
    render(<ControlledEditor initial="" loaded="origin = 'DEV'" />);

    fireEvent.change(box(), { target: { value: "origin = 'P" } });
    expect(box().value).toBe("origin = 'P");

    fireEvent.click(screen.getByRole("button", { name: "Load record" }));
    expect(box().value).toBe("origin = 'DEV'");
  });
});

describe("DatasetFilterEditor — Auto-indent", () => {
  it("reformats the clause in place and emits the formatted text", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(box(), {
      target: { value: "origin='PROD' AND ('urn:li:tag:a' IN tag_urns OR origin='DEV')" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Auto-indent" }));

    expect(box().value).toBe(
      "origin = 'PROD'\nAND (\n    'urn:li:tag:a' IN tag_urns\n    OR origin = 'DEV'\n)",
    );
    expect(lastEmitted(onEmit)).toBe(box().value);
  });

  it("does not reject text it cannot parse — the backend owns the grammar", () => {
    render(<ControlledEditor />);

    fireEvent.change(box(), { target: { value: "origin ==== " } });
    fireEvent.click(screen.getByRole("button", { name: "Auto-indent" }));

    expect(box().value).toBe("origin = = = =");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("leaves an empty clause empty", () => {
    // spec/API.md §`dataset_filter` grammar: the empty string is a valid filter
    // (all registered datasets) — Auto-indent must not invent text for it.
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.click(screen.getByRole("button", { name: "Auto-indent" }));

    expect(box().value).toBe("");
  });
});

describe("DatasetFilterEditor — validation is server-side only", () => {
  it("shows no inline error while an unparseable clause is being typed", () => {
    // "Validation is server-side" — the editor never runs the grammar itself, so
    // a clause the backend would reject still types (and emits) cleanly. The
    // error-prop test below is the backstop: the alert region does render when
    // the server reports one.
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(box(), { target: { value: "origin = 'PROD' AND (" } });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(lastEmitted(onEmit)).toBe("origin = 'PROD' AND (");
  });
});

describe("DatasetFilterEditor — server-side validation and grammar guide", () => {
  it("renders a 422 inline against the field, with the reported position", () => {
    render(
      <ControlledEditor
        error={{ message: "INVALID_DATASET_FILTER: unexpected token", position: 14 }}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("INVALID_DATASET_FILTER: unexpected token");
    expect(alert.textContent).toContain("14");
  });

  it("renders no inline error without one", () => {
    render(<ControlledEditor />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("folds the grammar guide beneath the box", () => {
    const { container } = render(<ControlledEditor />);
    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(screen.getByText("Filter grammar")).toBeInTheDocument();
  });
});
