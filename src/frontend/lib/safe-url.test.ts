/**
 * Display-link safety rule — JavaScript side of the cross-language drift guard.
 *
 * Spec anchor: `spec/API.md` §Data Resource → **Display-link safety**, whose
 * rule-class table (Scheme / Authority / Characters / Shape / Length) defines
 * every case asserted here.
 *
 * Why a client-side copy exists at all, per `spec/feature/FRONTEND_BASIC.md`
 * §Shell: "Both peripheral values are re-checked in the client against the
 * display-link safety rule ... and a failing value resolves to `""` — the same
 * 'render no link' state as an unset one. The client check backstops the API's
 * coercion at the point of interpolation, so the value is validated where it
 * actually becomes an `href`."
 *
 * The pattern-level cases all come from the shared corpus at
 * `tests/fixtures/safe-url-cases.json`, which `tests/unit/api/schemas/
 * test_safe_url_corpus.py` asserts against the two Python engines. Cases are NOT
 * hand-written here: a hand-written list is structurally unable to catch a
 * one-sided edit, which is the whole failure mode this file guards. Only the
 * length-boundary cases are local, because the corpus stores values verbatim and
 * a 512-character string does not belong in a reviewable fixture.
 */
import { describe, it, expect } from "vitest";
import corpusJson from "../../../tests/fixtures/safe-url-cases.json";
import {
  sanitizeDisplayUrl,
  sanitizeProjectId,
  SAFE_DISPLAY_URL_MAX_LENGTH,
  SAFE_PROJECT_ID_MAX_LENGTH,
} from "./safe-url";

// ── Corpus types ──────────────────────────────────────────────────────────────

interface Case {
  /** The spec rule-class row this case derives from. */
  rule: string;
  label: string;
  value: string;
}

interface Divergence extends Case {
  python_accepts: boolean;
  javascript_accepts: boolean;
  why: string[];
  remediation: string;
}

interface PatternCorpus {
  accept: Case[];
  reject: Case[];
  engine_divergence: Divergence[];
}

interface Corpus {
  _rule_classes: Record<string, string>;
  display_url: PatternCorpus;
  project_id: PatternCorpus;
}

const corpus = corpusJson as unknown as Corpus;

/** `[id, value]` rows for `it.each`, with the rule class in the test name. */
const rows = (cases: Case[]): [string, string][] =>
  cases.map((c) => [`${c.rule}: ${c.label}`, c.value]);

// ── Corpus integrity ──────────────────────────────────────────────────────────

describe("shared corpus — integrity", () => {
  // spec: spec/TESTING.md §Assertion Discipline — "Filter/query/matching tests
  //   seed both sides".
  it("seeds both accepted and rejected values for both patterns", () => {
    expect(corpus.display_url.accept.length).toBeGreaterThanOrEqual(10);
    expect(corpus.display_url.reject.length).toBeGreaterThanOrEqual(40);
    expect(corpus.project_id.accept.length).toBeGreaterThanOrEqual(5);
    expect(corpus.project_id.reject.length).toBeGreaterThanOrEqual(10);
  });

  // spec: spec/API.md §Data Resource → Display-link safety — the rule-class table.
  // A case that cannot cite a row asserts a property the spec does not grant.
  it("gives every case a rule class drawn from the spec table", () => {
    // "Slug" is the project_id sub-rule of the spec's Length row, split out so
    // that slug-grammar cases stop reporting the numeric length BOUND as covered.
    const expected = ["Authority", "Characters", "Length", "Scheme", "Shape", "Slug"];
    expect(Object.keys(corpus._rule_classes).sort()).toEqual(expected);

    const all = [
      ...corpus.display_url.accept,
      ...corpus.display_url.reject,
      ...corpus.project_id.accept,
      ...corpus.project_id.reject,
    ];
    for (const c of all) {
      expect(Object.keys(corpus._rule_classes), `${c.label} cites unknown rule ${c.rule}`).toContain(
        c.rule,
      );
    }
    // "Length" is deliberately absent from the corpus — a 512-character value is
    // not reviewable in a fixture — and is exercised by the local length-bounds
    // block below, mirrored by test_safe_url_corpus.py.
    const corpusExercised = [...new Set(all.map((c) => c.rule))].sort();
    expect(corpusExercised).toEqual(expected.filter((r) => r !== "Length"));
  });

  // spec: spec/API.md §Data Resource → Display-link safety, Shape row — "A path,
  //   query, or fragment must be introduced by `/`. This is a grammar constraint,
  //   not an anti-spoofing rule."
  it("pairs every Shape rejection with an accepted slash-introduced twin", () => {
    const shapeRejects = corpus.display_url.reject.filter((c) => c.rule === "Shape");
    const shapeAccepts = corpus.display_url.accept
      .filter((c) => c.rule === "Shape")
      .map((c) => c.value);
    expect(shapeRejects.length).toBeGreaterThan(0);

    for (const c of shapeRejects) {
      const introducers = [c.value.indexOf("?"), c.value.indexOf("#")].filter((i) => i !== -1);
      expect(introducers.length, `${c.value} has no query/fragment introducer`).toBeGreaterThan(0);
      const idx = Math.min(...introducers);
      const twin = c.value.slice(0, idx) + "/" + c.value.slice(idx);
      expect(
        shapeAccepts,
        `Shape rejection ${c.value} needs its accepted twin ${twin} in the corpus — ` +
          `without it the case reads as an anti-spoofing defence the guard lacks`,
      ).toContain(twin);
      expect(sanitizeDisplayUrl(twin)).toBe(twin);
    }
  });
});

// ── The rule, case by case ────────────────────────────────────────────────────

describe("sanitizeDisplayUrl", () => {
  // spec: spec/API.md §Data Resource → Display-link safety — a conforming value
  //   passes through; only a violating one is coerced.
  it.each(rows(corpus.display_url.accept))("accepts %s", (_id, value) => {
    expect(sanitizeDisplayUrl(value)).toBe(value);
  });

  // spec: spec/feature/FRONTEND_BASIC.md §Shell — "a failing value resolves to
  //   `""` — the same 'render no link' state as an unset one".
  it.each(rows(corpus.display_url.reject))("degrades %s to ''", (_id, value) => {
    expect(sanitizeDisplayUrl(value)).toBe("");
  });

  it("treats a nullish value as unset rather than invalid", () => {
    expect(sanitizeDisplayUrl(null)).toBe("");
    expect(sanitizeDisplayUrl(undefined)).toBe("");
  });
});

describe("sanitizeProjectId", () => {
  // spec: spec/API.md §Data Resource → Display-link safety, Length row —
  //   project_id "is further restricted to an alphanumeric slug" (the Slug
  //   sub-rule; the numeric bound is asserted in the length-bounds block below).
  it.each(rows(corpus.project_id.accept))("accepts %s", (_id, value) => {
    expect(sanitizeProjectId(value)).toBe(value);
  });

  it.each(rows(corpus.project_id.reject))("degrades %s to ''", (_id, value) => {
    expect(sanitizeProjectId(value)).toBe("");
  });

  it("treats a nullish value as unset rather than invalid", () => {
    expect(sanitizeProjectId(null)).toBe("");
    expect(sanitizeProjectId(undefined)).toBe("");
  });
});

// ── Length rule ───────────────────────────────────────────────────────────────
//
// Local rather than corpus-driven: the corpus stores values verbatim and a
// 512-character string is not reviewable in a fixture. The bounds themselves are
// exported constants shared with the backend, so a one-sided change to a bound
// still fails the Python suite's equivalent assertions.

describe("length bounds", () => {
  // spec: spec/API.md §Data Resource → Display-link safety, Length row —
  //   "Bounded — 512 characters for a URL, 256 for `project_id`".
  it("accepts a display URL exactly at the max length", () => {
    const prefix = "https://e.example.com/";
    const atLimit = prefix + "a".repeat(SAFE_DISPLAY_URL_MAX_LENGTH - prefix.length);
    expect(atLimit).toHaveLength(SAFE_DISPLAY_URL_MAX_LENGTH);
    expect(sanitizeDisplayUrl(atLimit)).toBe(atLimit);
  });

  it("degrades a display URL one character over the max length", () => {
    const prefix = "https://e.example.com/";
    const overLimit = prefix + "a".repeat(SAFE_DISPLAY_URL_MAX_LENGTH - prefix.length + 1);
    expect(overLimit).toHaveLength(SAFE_DISPLAY_URL_MAX_LENGTH + 1);
    expect(sanitizeDisplayUrl(overLimit)).toBe("");
  });

  it("accepts a project id exactly at the max length", () => {
    const atLimit = "a".repeat(SAFE_PROJECT_ID_MAX_LENGTH);
    expect(sanitizeProjectId(atLimit)).toBe(atLimit);
  });

  it("degrades a project id one character over the max length", () => {
    expect(sanitizeProjectId("a".repeat(SAFE_PROJECT_ID_MAX_LENGTH + 1))).toBe("");
  });
});

// ── Engine agreement ──────────────────────────────────────────────────────────

describe("shared corpus — engine divergence bucket", () => {
  // Asserts the state of the fixture, not of production code: a populated bucket
  // would mean someone recorded a known disagreement instead of fixing it. The
  // production guarantee comes from the case blocks above and their Python
  // counterparts asserting the SAME corpus — a one-sided regex edit fails one of
  // those, not this. The bucket stays in the schema as a documented home for a
  // future disagreement; see the corpus `_readme` for the shape an entry takes.
  it("records no remaining divergence between the three copies", () => {
    expect(corpus.display_url.engine_divergence).toEqual([]);
    expect(corpus.project_id.engine_divergence).toEqual([]);
  });
});

describe("sanitize is total and idempotent", () => {
  // spec: spec/API.md §Data Resource → Display-link safety — the guard coerces a
  //   violating value to `""`; it does not repair one. A sanitizer that returned
  //   a *modified* URL would satisfy every case above while silently rewriting an
  //   operator's configured host.
  it.each(rows([...corpus.display_url.accept, ...corpus.display_url.reject]))(
    "%s yields the input or ''",
    (_id, value) => {
      const once = sanitizeDisplayUrl(value);
      expect([value, ""]).toContain(once);
      expect(sanitizeDisplayUrl(once)).toBe(once);
    },
  );
});
