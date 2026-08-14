/**
 * Tests for DatasetFilterGuide — the folded, read-only grammar reference beneath the
 * DatasetFilterEditor. It renders no data and calls no route, so the only thing worth
 * asserting is that what it shows still agrees with the two things it mirrors: the
 * grammar fence in spec/API.md §`dataset_filter` grammar, and the canonical formatter
 * (`formatDatasetFilter`) every filter the editor emits is rendered through.
 *
 * A guide whose worked example does not survive a round-trip through that formatter
 * teaches a shape the editor immediately rewrites; a grammar block that drifts from
 * the spec fence teaches a grammar the parser rejects. Neither failure surfaces
 * anywhere else — the guide has no behaviour to break.
 *
 * Spec: spec/API.md §`dataset_filter` grammar,
 *       spec/feature/FRONTEND_BASIC.md §Shared component notes.
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { DatasetFilterGuide } from "./dataset-filter-guide";
import { formatDatasetFilter } from "@/lib/dataset-filter-format";

/** [grammar fence, worked example] — the guide's two `<pre>` blocks, in order. */
function guideBlocks(): [string, string] {
  const { container } = render(<DatasetFilterGuide />);
  const blocks = Array.from(container.querySelectorAll("pre")).map((pre) => pre.textContent ?? "");
  expect(
    blocks.length,
    "the guide renders exactly two pre blocks: the grammar and the worked example",
  ).toBe(2);
  return [blocks[0]!, blocks[1]!];
}

describe("DatasetFilterGuide", () => {
  it("reproduces the spec's grammar fence line for line", () => {
    // The whole block, not a handful of substrings: the fence's trailing `--` notes
    // are the part a reader relies on (`bare word, never quoted` is the only place
    // the guide says a boolean is unquoted), and a prefix match would let any of them
    // be dropped silently. Transcribed from the fenced block in spec/API.md
    // §`dataset_filter` grammar, which this is byte-identical to except the `term`
    // line: the spec's "(see below)" points at a §Nesting depth paragraph the guide
    // does not reproduce.
    const [grammar] = guideBlocks();

    expect(grammar.split("\n")).toEqual([
      "filter      := ε | expr                        -- empty string = all registered datasets",
      "expr        := term { (AND|OR) term }           -- one operator kind per level",
      "term        := predicate | '(' expr ')'         -- parens nest at most 2 deep",
      "predicate   := scalar_col '=' string",
      "             | scalar_col IN '(' string {',' string} ')'",
      "             | string IN array_col",
      "             | bool_col '=' bool",
      "scalar_col  := dataset_urn | origin | platform_urn",
      "array_col   := tag_urns | glossary_term_urns",
      "bool_col    := is_primary",
      "bool        := TRUE | FALSE                     -- bare word, never quoted",
      "string      := '...'                            -- single quotes only; '' escapes a quote",
    ]);
  });

  it("lists is_primary in the column reference with its kind and meaning", () => {
    // The grammar fence names the production; the column list beneath it is where a
    // reader learns what the column means, so the entry has to exist and carry the
    // kind the grammar partitions it into.
    // spec/API.md §`dataset_filter` grammar — column table row: "`is_primary` | bool |
    // `true` when the dataset is the primary member of its DataHub sibling set, or has
    // no siblings".
    const { getByText } = render(<DatasetFilterGuide />);

    const entry = getByText("is_primary").closest("li");
    expect(entry, "is_primary must appear in the guide's column list").not.toBeNull();
    expect(entry!.textContent).toContain("(bool)");
    expect(entry!.textContent).toContain("primary member of its DataHub sibling set");
    expect(entry!.textContent).toContain("no sibling");
  });

  it("shows a worked example that is already in canonical form", () => {
    // The editor rewrites whatever it is given through `formatDatasetFilter`
    // (components/dataset-filter-editor.tsx), so an example the formatter would
    // reshape is an example the user cannot type and keep.
    const [, example] = guideBlocks();

    expect(example).toContain("AND is_primary = true");
    expect(formatDatasetFilter(example)).toBe(example);
  });

  it("shows the example a flat clause formats into", () => {
    // The complement of the round-trip above: the same clause written on one line —
    // which is how a filter arrives from an API payload — formats to exactly the
    // block the guide prints, so the guide is showing the canonical form rather than
    // one arbitrary layout among several.
    const [, example] = guideBlocks();
    const flat =
      "origin = 'PROD' AND is_primary = true AND ('urn:li:tag:area:catalog' IN tag_urns " +
      "OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)";

    expect(flat).not.toBe(example); // backstop: the input really is the unformatted form
    expect(formatDatasetFilter(flat)).toBe(example);
  });
});
