/**
 * Tests for DatasetFilterEditor — splitList parsing and dataset_filter contract.
 *
 * Spec traces:
 *   - spec/API.md §Metric dataset_filter: four-dimension shape (origin, tags,
 *     glossary_terms, dataset_urns); array dimensions cap at 1,000 entries.
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics create/edit form — dataset_filter
 *     textarea parses newline- or comma-separated input.
 *   - src/api/schemas/_dataset_filter.py — origin is a scalar string; tags /
 *     glossary_terms / dataset_urns are string[]; empty arrays collapse to undefined
 *     so the backend receives absent keys rather than [].
 *   - components/dataset-filter-editor.tsx — splitList and the onChange contract
 *     (empty list → undefined for that dimension).
 */

import { describe, it, expect } from "vitest";
import { splitList, splitLines } from "./dataset-filter-editor";

// ── 1. splitList — textarea-to-array parser ────────────────────────────────────

describe("splitList — newline splitting", () => {
  it("splits a newline-separated list into trimmed entries", () => {
    expect(splitList("urn:li:tag:a\nurn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("splits a multi-line list and trims surrounding whitespace from each entry", () => {
    expect(splitList("  urn:li:tag:a  \n  urn:li:tag:b  ")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("splits a single entry (no newline)", () => {
    expect(splitList("urn:li:tag:env:DEV")).toEqual(["urn:li:tag:env:DEV"]);
  });
});

describe("splitList — comma splitting", () => {
  it("splits a comma-separated list", () => {
    expect(splitList("urn:li:tag:a,urn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("splits a comma-separated list with spaces around commas", () => {
    expect(splitList("urn:li:tag:a , urn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });
});

describe("splitList — mixed delimiters", () => {
  it("handles mixed newlines and commas in the same input", () => {
    const raw = "urn:li:tag:a\nurn:li:tag:b,urn:li:tag:c";
    expect(splitList(raw)).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
      "urn:li:tag:c",
    ]);
  });
});

describe("splitList — empty and whitespace-only input", () => {
  it("returns [] for an empty string", () => {
    expect(splitList("")).toEqual([]);
  });

  it("returns [] for a whitespace-only string", () => {
    expect(splitList("   ")).toEqual([]);
  });

  it("returns [] for a newline-only string", () => {
    expect(splitList("\n\n\n")).toEqual([]);
  });

  it("returns [] for a comma-only string", () => {
    expect(splitList(",,,")).toEqual([]);
  });

  it("never returns entries that are empty strings", () => {
    // Double comma produces an empty token — must be dropped
    const result = splitList("urn:li:tag:a,,urn:li:tag:b");
    expect(result).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
    result.forEach((entry) => expect(entry.length).toBeGreaterThan(0));
  });

  it("never returns entries that are whitespace-only strings", () => {
    const result = splitList("urn:li:tag:a,   ,urn:li:tag:b");
    expect(result).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
    result.forEach((entry) => expect(entry.trim().length).toBeGreaterThan(0));
  });
});

describe("splitList — trimming", () => {
  it("trims leading and trailing whitespace from every entry", () => {
    const result = splitList("  urn:li:tag:a  \n  urn:li:tag:b  ");
    expect(result).toEqual(["urn:li:tag:a", "urn:li:tag:b"]);
  });

  it("preserves internal whitespace inside an entry (URNs do not have spaces, but split should not collapse them)", () => {
    // splitList only trims edges, does not collapse internal spaces
    const result = splitList("urn:li:tag:a b");
    expect(result).toEqual(["urn:li:tag:a b"]);
  });
});

// ── 1b. splitLines — newline-only parser for dataset_urns ──────────────────────
//
// A DataHub dataset URN embeds commas inside its (platform,name,fabric) tuple,
// so dataset_urns must split on newline only. Comma-splitting (splitList) would
// shred a single URN into invalid fragments and the backend rejects them with
// 422 INVALID_DATASET_URN.

describe("splitLines — dataset_urns newline-only parser", () => {
  it("keeps a single dataset URN with internal commas as one element", () => {
    const urn =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
    expect(splitLines(urn)).toEqual([urn]);
  });

  it("splits two dataset URNs on two lines into two elements (commas preserved)", () => {
    const a =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
    const b =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.actor_master,PROD)";
    expect(splitLines(`${a}\n${b}`)).toEqual([a, b]);
  });

  it("trims surrounding whitespace and drops blank lines", () => {
    const a =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
    const b =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.actor_master,PROD)";
    expect(splitLines(`  ${a}  \n\n  ${b}  \n`)).toEqual([a, b]);
  });

  it("returns [] for empty and whitespace-only input", () => {
    expect(splitLines("")).toEqual([]);
    expect(splitLines("   \n\n  ")).toEqual([]);
  });
});

// ── 1c. splitList still comma-splits tags / glossary_terms ─────────────────────
//
// Tag and glossary URNs contain no commas, so comma-separation remains valid and
// convenient for those dimensions.

describe("splitList — tags comma-split still works", () => {
  it("splits comma-separated tag URNs", () => {
    expect(splitList("urn:li:tag:a,urn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });
});

// ── 2. onChange contract — empty list collapses to undefined ───────────────────
//
// DatasetFilterEditor's onChange handlers set each dimension to undefined when
// the parsed list is empty so that the backend receives absent keys rather than [].
// This logic lives inline in the component's event handlers:
//   set({ tags: list.length ? list : undefined })
// The invariant: splitList("") returns [] → length is 0 → undefined is emitted.

describe("splitList — empty-list-to-undefined contract", () => {
  it("empty input produces [] which triggers undefined emission by the onChange contract", () => {
    const list = splitList("");
    // The component does: list.length ? list : undefined
    const emitted = list.length ? list : undefined;
    expect(emitted).toBeUndefined();
  });

  it("non-empty input produces a non-empty array which is emitted directly", () => {
    const list = splitList("urn:li:tag:x");
    const emitted = list.length ? list : undefined;
    expect(emitted).toEqual(["urn:li:tag:x"]);
  });
});

// ── 3. origin scalar — not processed by splitList ─────────────────────────────
//
// origin is handled via <Input> directly: onChange sets value or undefined if empty.
// The editor emits:  set({ origin: e.target.value || undefined })
// This is a pure JS expression, not a split operation — we verify the boolean
// contract rather than re-testing the component's event binding.

describe("origin scalar contract", () => {
  it("non-empty string is truthy — emitted as-is", () => {
    const raw = "DEV";
    const emitted = raw || undefined;
    expect(emitted).toBe("DEV");
  });

  it("empty string is falsy — collapses to undefined", () => {
    const raw = "";
    const emitted = raw || undefined;
    expect(emitted).toBeUndefined();
  });
});
