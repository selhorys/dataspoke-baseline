/**
 * Tests for formatDatasetFilter — the Auto-indent formatter behind
 * DatasetFilterEditor.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor:
 *     "An **Auto-indent** button reformats the text in place: newline before each
 *     top-level `AND` / `OR`, indent inside parentheses. The formatter is purely
 *     lexical and holds **no grammar knowledge** — the backend owns the grammar,
 *     so the button never rejects, rewrites, or silently repairs a clause it
 *     cannot understand."
 *   - spec/API.md §`dataset_filter` grammar — the clause shapes being laid out
 *     (both `IN` forms, `''` as an escaped quote, parens nesting).
 *
 * Two families of assertion, matching the two halves of that spec sentence:
 *
 *   1. **Layout** — the canonical form. The reference is `format_filter()` in
 *      src/shared/dataset_filter.py, whose docstring names itself "the executable
 *      reference for the layout the frontend's Auto-indent button produces". The
 *      expected strings below were taken from that function's output for the same
 *      input, so this file pins the TS side to the documented canonical form
 *      rather than to whatever the TS implementation happens to emit. Parity is
 *      claimed only for **already-canonical** input: the Python side additionally
 *      normalises keyword/column case and drops redundant parens, which is grammar
 *      knowledge the lexical TS formatter is specified not to have (family 2).
 *
 *   2. **No grammar knowledge** — token text survives verbatim (keyword case,
 *      column case, redundant parens, string contents) and unparseable text is
 *      passed through rather than rejected or repaired.
 *
 * Mocked: nothing — the formatter is pure.
 */

import { describe, it, expect } from "vitest";
import { formatDatasetFilter, tokenizeDatasetFilter } from "./dataset-filter-format";

/** The token stream, as `kind:text` pairs — what must survive formatting. */
function tokens(text: string): string[] {
  return tokenizeDatasetFilter(text).map((t) => `${t.kind}:${t.text}`);
}

// ── 1. Layout — the canonical form (pinned to src/shared/dataset_filter.py) ────

describe("formatDatasetFilter — canonical layout", () => {
  it("puts each top-level AND on its own line, operator-leading", () => {
    // spec/feature/FRONTEND_BASIC.md: "newline before each top-level AND / OR".
    expect(
      formatDatasetFilter("origin = 'PROD' AND 'urn:li:tag:area:catalog' IN tag_urns"),
    ).toBe("origin = 'PROD'\nAND 'urn:li:tag:area:catalog' IN tag_urns");
  });

  it("indents a parenthesised group's body and closes the paren at the parent indent", () => {
    // The API.md §`dataset_filter` grammar example, laid out. Byte-identical to
    // format_filter()'s output for the same clause (src/shared/dataset_filter.py).
    expect(
      formatDatasetFilter(
        "origin = 'PROD' AND ('urn:li:tag:area:catalog' IN tag_urns" +
          " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)",
      ),
    ).toBe(
      "origin = 'PROD'\n" +
        "AND (\n" +
        "    'urn:li:tag:area:catalog' IN tag_urns\n" +
        "    OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns\n" +
        ")",
    );
  });

  it("indents a second paren level one step further", () => {
    // spec/API.md §`dataset_filter` grammar — "parens nest at most 2 deep";
    // `a AND (b OR (c AND d))` is the depth-2 clause the spec calls accepted.
    expect(formatDatasetFilter("origin = 'A' AND (origin = 'B' OR (origin = 'C' AND origin = 'D'))")).toBe(
      "origin = 'A'\n" +
        "AND (\n" +
        "    origin = 'B'\n" +
        "    OR (\n" +
        "        origin = 'C'\n" +
        "        AND origin = 'D'\n" +
        "    )\n" +
        ")",
    );
  });

  it("keeps an IN value list on one line, comma-space separated", () => {
    // The `scalar_col IN '(' string {',' string} ')'` predicate is one operand,
    // not a group — its parens must not break across lines.
    expect(formatDatasetFilter("origin IN ('PROD','DEV')")).toBe("origin IN ('PROD', 'DEV')");
  });

  it("spaces the `=` operator of a predicate", () => {
    expect(formatDatasetFilter("origin='PROD'")).toBe("origin = 'PROD'");
  });

  it("renders an empty or whitespace-only clause as the empty string", () => {
    // spec/API.md §`dataset_filter` grammar: "filter := ε | expr — empty string =
    // all registered datasets". Auto-indent must not invent text for it.
    expect(formatDatasetFilter("")).toBe("");
    expect(formatDatasetFilter("   \n  ")).toBe("");
  });

  it("is idempotent — formatting canonical text changes nothing", () => {
    const canonical =
      "origin = 'PROD'\n" +
      "AND (\n" +
      "    'urn:li:tag:area:catalog' IN tag_urns\n" +
      "    OR origin IN ('DEV', 'STG')\n" +
      ")";
    expect(formatDatasetFilter(canonical)).toBe(canonical);
  });
});

// ── 2. Purely lexical — no grammar knowledge ──────────────────────────────────

describe("formatDatasetFilter — purely lexical, no grammar knowledge", () => {
  it("preserves keyword case instead of normalising it", () => {
    // The backend's format_filter() uppercases `and` → `AND`; the client is
    // specified to hold no grammar knowledge, so it only re-lays-out.
    expect(formatDatasetFilter("origin = 'PROD' and origin = 'DEV'")).toBe(
      "origin = 'PROD'\nand origin = 'DEV'",
    );
  });

  it("preserves column-name case", () => {
    expect(formatDatasetFilter("ORIGIN = 'PROD'")).toBe("ORIGIN = 'PROD'");
  });

  it("keeps redundant parentheses rather than simplifying them", () => {
    expect(formatDatasetFilter("((origin = 'A'))")).toBe(
      "(\n    (\n        origin = 'A'\n    )\n)",
    );
  });

  it("preserves a '' escaped quote inside a literal", () => {
    // spec/API.md §`dataset_filter` grammar: "single quotes only; '' escapes a quote".
    expect(formatDatasetFilter("origin = 'O''Brien'")).toBe("origin = 'O''Brien'");
  });

  it("does not treat a comma or paren inside a literal as punctuation", () => {
    // A dataset URN carries both — mis-lexing one would split the literal.
    const urnClause =
      "dataset_urn IN ('urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)')";
    expect(formatDatasetFilter(urnClause)).toBe(urnClause);
  });

  it("passes unbalanced parentheses through without repairing them", () => {
    // "never rejects, rewrites, or silently repairs a clause it cannot
    // understand" — the missing `)` stays missing for the backend to report.
    expect(formatDatasetFilter("origin = 'PROD' AND (")).toBe("origin = 'PROD'\nAND (");
  });

  it("passes an unterminated string literal through verbatim", () => {
    expect(formatDatasetFilter("origin = 'unterminated")).toBe("origin = 'unterminated");
  });

  it("lays out text that is not a clause at all instead of rejecting it", () => {
    expect(formatDatasetFilter("origin ==== ")).toBe("origin = = = =");
  });
});

// ── 3. Token preservation and idempotence over the whole corpus ───────────────

describe("formatDatasetFilter — only whitespace changes", () => {
  const CORPUS = [
    "",
    "origin='PROD'",
    "origin = 'PROD' AND 'urn:li:tag:area:catalog' IN tag_urns",
    "origin = 'PROD' AND ('urn:li:tag:a' IN tag_urns OR origin = 'DEV')",
    "origin IN ('PROD','DEV')",
    "origin = 'O''Brien'",
    "((origin = 'A'))",
    "ORIGIN = 'PROD' and X",
    "origin = 'PROD' AND (",
    "origin = 'unterminated",
    "origin ==== ",
    "dataset_urn IN ('urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)')",
  ];

  it.each(CORPUS)("re-emits the same token stream for %j", (clause) => {
    // The formatter's whole contract: same tokens, different whitespace. If a
    // token were dropped, added, or rewritten, the clause's meaning would change
    // behind the user's back — the "never rewrites" half of the spec sentence.
    expect(tokens(formatDatasetFilter(clause))).toEqual(tokens(clause));
  });

  it.each(CORPUS)("is idempotent for %j", (clause) => {
    const once = formatDatasetFilter(clause);
    expect(formatDatasetFilter(once)).toBe(once);
  });
});
