/**
 * Tests for splitList — the textarea-to-array parser behind the MetaGen
 * RunDialog's `dataset_urns` override.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Components (RunDialog) — "one URN per
 * line, edge-trimmed, blank lines dropped. Commas are not separators, since a
 * dataset URN always contains them."
 */
import { describe, it, expect } from "vitest";
import { splitList } from "./urn-list";

// A real dataset URN — commas are structural inside the (platform,name,fabric)
// tuple, which is what makes comma-splitting destructive here.
const DATASET_URN_A =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
const DATASET_URN_B =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)";

describe("splitList — newline is the only separator", () => {
  it("splits a newline-separated list into entries", () => {
    expect(splitList("urn:li:tag:a\nurn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("tolerates CRLF line endings", () => {
    expect(splitList("urn:li:tag:a\r\nurn:li:tag:b")).toEqual([
      "urn:li:tag:a",
      "urn:li:tag:b",
    ]);
  });

  it("keeps a comma inside one entry — it is not a separator", () => {
    expect(splitList("urn:li:tag:area,catalog")).toEqual(["urn:li:tag:area,catalog"]);
    expect(splitList(DATASET_URN_A)).toEqual([DATASET_URN_A]);
    expect(splitList(`${DATASET_URN_A}\n${DATASET_URN_B}`)).toEqual([
      DATASET_URN_A,
      DATASET_URN_B,
    ]);
  });

  it("edge-trims each line and drops blank lines", () => {
    expect(splitList(`  ${DATASET_URN_A}  \n\n  ${DATASET_URN_B}  \n`)).toEqual([
      DATASET_URN_A,
      DATASET_URN_B,
    ]);
  });

  it("preserves whitespace inside a line", () => {
    expect(splitList("urn:li:tag:a b")).toEqual(["urn:li:tag:a b"]);
  });

  it("returns an empty list for empty or whitespace-only input", () => {
    expect(splitList("")).toEqual([]);
    expect(splitList("   ")).toEqual([]);
    expect(splitList("\n\n\n")).toEqual([]);
  });
});
