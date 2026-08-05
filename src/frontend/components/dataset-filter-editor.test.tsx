/**
 * Tests for DatasetFilterEditor — the newline-separated input contract of the
 * four-dimension dataset_filter, at both the parser and the render level.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor:
 *     "Each list dimension is one newline-separated textarea — one URN per line —
 *     buffering the raw text the user typed; parsing happens on the way out (each
 *     line edge-trimmed, blank lines dropped, an empty dimension omitted from the
 *     filter) and parsed state is never re-serialised back into the box, so
 *     whitespace the user is mid-way through typing survives. Commas are not
 *     separators: tag and glossary-term URNs embed a user-authored name that may
 *     contain a comma, and dataset URNs always contain them. The editor reseeds
 *     its boxes from props only when the incoming filter is not the one it last
 *     emitted (e.g. a freshly loaded record)."
 *   - spec/API.md §Metric conf fields: `dataset_filter` is
 *     `{origin?, tags?[], glossary_terms?[], dataset_urns?[]}`; `{}` = all datasets.
 *   - spec/API.md §Error codes → INVALID_DATASET_URN: a `dataset_urns` entry must be
 *     a well-formed `urn:li:dataset:(…)` URN — a comma-split fragment of one is not.
 *
 * Mocked: nothing — the component is pure client state. Vitest unit tier.
 */
import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { DatasetFilterEditor, splitList } from "./dataset-filter-editor";
import type { DatasetFilter } from "@/types/governance";

// A real dataset URN — commas are structural inside the (platform,name,fabric)
// tuple, which is what makes comma-splitting destructive here.
const DATASET_URN_A =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
const DATASET_URN_B =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)";

// ── 1. splitList — the shared textarea-to-array parser ─────────────────────────

describe("splitList — newline is the only separator", () => {
  it("splits a newline-separated list into entries", () => {
    expect(splitList("urn:li:tag:a\nurn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("returns a single entry when there is no newline", () => {
    expect(splitList("urn:li:tag:env:DEV")).toEqual(["urn:li:tag:env:DEV"]);
  });

  it("tolerates CRLF line endings", () => {
    expect(splitList("urn:li:tag:a\r\nurn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("keeps a tag URN containing a comma as one entry", () => {
    expect(splitList("urn:li:tag:area,catalog")).toEqual(["urn:li:tag:area,catalog"]);
  });

  it("keeps a glossary term URN containing a comma as one entry", () => {
    expect(splitList("urn:li:glossaryTerm:Finance,Revenue")).toEqual([
      "urn:li:glossaryTerm:Finance,Revenue",
    ]);
  });

  it("keeps a dataset URN's structural commas inside one entry", () => {
    expect(splitList(DATASET_URN_A)).toEqual([DATASET_URN_A]);
  });

  it("splits two comma-bearing dataset URNs on two lines into exactly two entries", () => {
    expect(splitList(`${DATASET_URN_A}\n${DATASET_URN_B}`)).toEqual([
      DATASET_URN_A,
      DATASET_URN_B,
    ]);
  });
});

describe("splitList — edge trim, blank lines, empty input", () => {
  it("edge-trims each line and drops blank lines between entries", () => {
    expect(splitList(`  ${DATASET_URN_A}  \n\n  ${DATASET_URN_B}  \n`)).toEqual([
      DATASET_URN_A,
      DATASET_URN_B,
    ]);
  });

  it("preserves whitespace inside an entry", () => {
    expect(splitList("urn:li:tag:a b")).toEqual(["urn:li:tag:a b"]);
  });

  it("returns [] for an empty string", () => {
    expect(splitList("")).toEqual([]);
  });

  it("returns [] for a whitespace-only string", () => {
    expect(splitList("   ")).toEqual([]);
  });

  it("returns [] for a newline-only string", () => {
    expect(splitList("\n\n\n")).toEqual([]);
  });

  it("never yields a blank entry from an interior blank or whitespace-only line", () => {
    const result = splitList("urn:li:tag:a\n\n   \nurn:li:tag:b");
    expect(result).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
    result.forEach((entry) => expect(entry.trim().length).toBeGreaterThan(0));
  });
});

// ── 2. Render level — the parent-owned editing loop ───────────────────────────
//
// The editor is parent-owned: it emits a parsed DatasetFilter and the parent
// feeds it back down as `value`. That echo is exactly the path that used to
// clobber in-progress whitespace, so the harness below reproduces it faithfully
// (emit → setState → re-render) rather than rendering the editor uncontrolled.

/** Mirrors the real parents (metric form via `watch`, conf pages via useState). */
function ControlledEditor({
  initial = {},
  onEmit,
  loaded,
  cloneEcho = false,
}: {
  initial?: DatasetFilter;
  onEmit?: (v: DatasetFilter) => void;
  /** A record "loaded from the API" — applied by the Load record button. */
  loaded?: DatasetFilter;
  /**
   * Echo a structurally-equal but freshly-allocated filter, as a parent that
   * normalises its form state or refetches without structural sharing does. The
   * editor's whitespace guarantee is its own property, not a consequence of a
   * parent happening to preserve object identity.
   */
  cloneEcho?: boolean;
}) {
  const [filter, setFilter] = useState<DatasetFilter>(initial);
  return (
    <>
      <DatasetFilterEditor
        value={filter}
        onChange={(v) => {
          onEmit?.(v);
          setFilter(cloneEcho ? structuredClone(v) : v);
        }}
      />
      {loaded && (
        <button type="button" onClick={() => setFilter(loaded)}>
          Load record
        </button>
      )}
    </>
  );
}

function tagsBox(): HTMLTextAreaElement {
  return screen.getByLabelText("tags") as HTMLTextAreaElement;
}

function glossaryTermsBox(): HTMLTextAreaElement {
  return screen.getByLabelText("glossary_terms") as HTMLTextAreaElement;
}

function datasetUrnsBox(): HTMLTextAreaElement {
  return screen.getByLabelText("dataset_urns") as HTMLTextAreaElement;
}

function originBox(): HTMLInputElement {
  return screen.getByLabelText("origin") as HTMLInputElement;
}

function lastEmitted(onEmit: ReturnType<typeof vi.fn>): DatasetFilter {
  expect(onEmit).toHaveBeenCalled();
  return onEmit.mock.calls[onEmit.mock.calls.length - 1][0] as DatasetFilter;
}

describe("DatasetFilterEditor — the box holds raw text, parsing happens on the way out", () => {
  it("keeps a trailing newline in the box while emitting one trimmed entry, so the next line can be typed", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n" } });

    // The raw text the user typed survives the parent's echo of the parsed value.
    expect(tagsBox().value).toBe("urn:li:tag:a\n");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);

    // …and the user can go on to type the second line.
    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\nurn:li:tag:b" } });
    expect(tagsBox().value).toBe("urn:li:tag:a\nurn:li:tag:b");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
  });

  it("keeps a trailing space in the box while emitting the edge-trimmed entry", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a " } });

    expect(tagsBox().value).toBe("urn:li:tag:a ");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);
  });

  it("keeps an interior blank line in the box while dropping it from the emitted list", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n\n" } });
    expect(tagsBox().value).toBe("urn:li:tag:a\n\n");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n\nurn:li:tag:b" } });
    expect(tagsBox().value).toBe("urn:li:tag:a\n\nurn:li:tag:b");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
  });

  it("preserves whitespace inside a URN end-to-end", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:two words" } });

    expect(tagsBox().value).toBe("urn:li:tag:two words");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:two words"]);
  });

  it("keeps the trailing newline even when the parent echoes a freshly allocated equal filter", () => {
    // Object identity of the echoed filter is the parent's business: a parent
    // that normalises its form state, or refetches without structural sharing,
    // hands back an equal-but-different object. The box must still hold the raw
    // text either way.
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} cloneEcho />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n" } });

    expect(tagsBox().value).toBe("urn:li:tag:a\n");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\nurn:li:tag:b " } });
    expect(tagsBox().value).toBe("urn:li:tag:a\nurn:li:tag:b ");
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
  });
});

describe("DatasetFilterEditor — every dimension of one filter is editable at once", () => {
  // spec/API.md §Metric conf fields: the three list dimensions "are OR-ed among
  // themselves and AND-ed with `origin`", so one filter legitimately carries
  // several dimensions simultaneously — each box buffers its own raw text.
  it("keeps each box's raw text while composing origin + all three lists into one filter", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(originBox(), { target: { value: "DEV" } });
    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n" } });
    fireEvent.change(glossaryTermsBox(), {
      target: { value: "urn:li:glossaryTerm:Finance,Revenue" },
    });
    fireEvent.change(datasetUrnsBox(), { target: { value: `${DATASET_URN_A}\n` } });

    // Every box still reads back exactly what was typed into it — filling one
    // dimension does not disturb the others' buffers.
    expect(originBox().value).toBe("DEV");
    expect(tagsBox().value).toBe("urn:li:tag:a\n");
    expect(glossaryTermsBox().value).toBe("urn:li:glossaryTerm:Finance,Revenue");
    expect(datasetUrnsBox().value).toBe(`${DATASET_URN_A}\n`);

    // …and the one emitted filter carries all four dimensions together.
    expect(lastEmitted(onEmit)).toEqual({
      origin: "DEV",
      tags: ["urn:li:tag:a"],
      glossary_terms: ["urn:li:glossaryTerm:Finance,Revenue"],
      dataset_urns: [DATASET_URN_A],
    });
  });

  it("appends to a dimension typed earlier without dropping the ones typed since", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a\n" } });
    fireEvent.change(glossaryTermsBox(), { target: { value: "urn:li:glossaryTerm:Rev" } });
    fireEvent.change(datasetUrnsBox(), { target: { value: DATASET_URN_B } });

    // The user returns to the tags box and types the second line onto what is
    // already there — reading the live box content, as a real keystroke does.
    fireEvent.change(tagsBox(), { target: { value: `${tagsBox().value}urn:li:tag:b` } });

    expect(tagsBox().value).toBe("urn:li:tag:a\nurn:li:tag:b");
    expect(lastEmitted(onEmit)).toEqual({
      tags: ["urn:li:tag:a", "urn:li:tag:b"],
      glossary_terms: ["urn:li:glossaryTerm:Rev"],
      dataset_urns: [DATASET_URN_B],
    });
  });
});

describe("DatasetFilterEditor — commas are not separators", () => {
  it("emits one entry for a pasted dataset URN and two for two lines of them", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    // One URN: its three structural commas must not shred it into fragments.
    fireEvent.change(datasetUrnsBox(), { target: { value: DATASET_URN_A } });
    expect(lastEmitted(onEmit).dataset_urns).toEqual([DATASET_URN_A]);

    // Two URNs: the newline — and only the newline — separates them.
    fireEvent.change(datasetUrnsBox(), {
      target: { value: `${DATASET_URN_A}\n${DATASET_URN_B}` },
    });
    expect(lastEmitted(onEmit).dataset_urns).toEqual([DATASET_URN_A, DATASET_URN_B]);
  });

  it("emits a comma-bearing tag URN as one entry", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:area,catalog" } });

    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:area,catalog"]);
  });

  it("emits a comma-bearing glossary term URN as one entry", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(glossaryTermsBox(), {
      target: { value: "urn:li:glossaryTerm:Finance,Revenue" },
    });

    expect(lastEmitted(onEmit).glossary_terms).toEqual([
      "urn:li:glossaryTerm:Finance,Revenue",
    ]);
  });
});

describe("DatasetFilterEditor — an empty dimension is omitted from the filter", () => {
  it("drops the key once a populated dimension is cleared", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    // Backstop: the dimension was populated first, so the absence below is not vacuous.
    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a" } });
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);

    fireEvent.change(tagsBox(), { target: { value: "" } });
    expect(lastEmitted(onEmit).tags).toBeUndefined();
    // The request body carries an absent key, not [].
    expect(JSON.parse(JSON.stringify(lastEmitted(onEmit)))).not.toHaveProperty("tags");
  });

  it("drops the key for a whitespace-only box", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:a" } });
    expect(lastEmitted(onEmit).tags).toEqual(["urn:li:tag:a"]);

    fireEvent.change(tagsBox(), { target: { value: "  \n \n" } });
    expect(tagsBox().value).toBe("  \n \n"); // still editable text
    expect(lastEmitted(onEmit).tags).toBeUndefined();
  });
});

describe("DatasetFilterEditor — origin is a scalar", () => {
  it("emits the typed origin and drops it when cleared", () => {
    const onEmit = vi.fn();
    render(<ControlledEditor onEmit={onEmit} />);

    fireEvent.change(originBox(), { target: { value: "DEV" } });
    expect(lastEmitted(onEmit).origin).toBe("DEV");

    fireEvent.change(originBox(), { target: { value: "" } });
    expect(lastEmitted(onEmit).origin).toBeUndefined();
  });
});

describe("DatasetFilterEditor — the field hints state the separator rule", () => {
  // The hints are the only place the newline-only rule is stated to the user;
  // steering someone back to `urn:li:tag:a,urn:li:tag:b` yields one malformed
  // entry (422 INVALID_DATASET_URN for dataset_urns, a silent no-match for tags).
  /** The help text rendered beneath one list box, per Field's hint slot. */
  function hintFor(box: HTMLTextAreaElement): string {
    const field = box.parentElement as HTMLElement;
    const paragraphs = Array.from(field.querySelectorAll("p"));
    expect(paragraphs.length).toBeGreaterThan(0); // backstop: a hint is rendered at all
    return paragraphs.map((p) => p.textContent ?? "").join(" ");
  }

  it.each([
    ["tags", tagsBox],
    ["glossary_terms", glossaryTermsBox],
    ["dataset_urns", datasetUrnsBox],
  ])("tells the user %s takes one URN per line, not comma-separated values", (_label, box) => {
    render(<ControlledEditor />);

    const hint = hintFor(box());
    expect(hint).toMatch(/one per line/i);
    expect(hint).not.toMatch(/comma[- ]separated/i);
  });
});

describe("DatasetFilterEditor — reseeding from props", () => {
  it("seeds each box from the initial filter, one entry per line", () => {
    render(
      <ControlledEditor
        initial={{
          origin: "DEV",
          tags: ["urn:li:tag:a", "urn:li:tag:area,catalog"],
          dataset_urns: [DATASET_URN_A, DATASET_URN_B],
        }}
      />,
    );

    expect(tagsBox().value).toBe("urn:li:tag:a\nurn:li:tag:area,catalog");
    expect(datasetUrnsBox().value).toBe(`${DATASET_URN_A}\n${DATASET_URN_B}`);
    expect(originBox().value).toBe("DEV");
  });

  it("reseeds the boxes when the parent supplies a freshly loaded record", () => {
    render(
      <ControlledEditor
        initial={{}}
        loaded={{ origin: "PROD", tags: ["urn:li:tag:loaded"], dataset_urns: [DATASET_URN_B] }}
      />,
    );

    // The user has unsaved text in the box when the record lands.
    fireEvent.change(tagsBox(), { target: { value: "urn:li:tag:typed\n" } });
    expect(tagsBox().value).toBe("urn:li:tag:typed\n");

    fireEvent.click(screen.getByRole("button", { name: "Load record" }));

    expect(tagsBox().value).toBe("urn:li:tag:loaded");
    expect(datasetUrnsBox().value).toBe(DATASET_URN_B);
    expect(originBox().value).toBe("PROD");
  });

  it("clears a box when the loaded record has no entries for that dimension", () => {
    render(
      <ControlledEditor
        initial={{ tags: ["urn:li:tag:a"] }}
        loaded={{ dataset_urns: [DATASET_URN_A] }}
      />,
    );

    expect(tagsBox().value).toBe("urn:li:tag:a");

    fireEvent.click(screen.getByRole("button", { name: "Load record" }));

    expect(tagsBox().value).toBe("");
    expect(datasetUrnsBox().value).toBe(DATASET_URN_A);
  });
});
