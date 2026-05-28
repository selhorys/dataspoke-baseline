/**
 * Tests for lib/metagen-predicates.ts — all four exported predicates.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_METAGEN.md §Page contracts:
 *     "reject only valid on llm_approved"; "finalized items collapse to a single approved row";
 *     "confirm dialog labels the destination DataHub aspect"
 *   - src/api/schemas/metagen.py MetagenCandidate.status:
 *     "llm_approved" | "approved" | "rejected"
 *   - src/api/schemas/metagen.py MetagenItemSummary.status:
 *     "pending" | "llm_approved" | "approved"
 *   - src/api/schemas/metagen.py MetagenBoundaryPutRequest.allowed / MetagenItemSummary.kind:
 *     "dataset.description" | "column.description"
 *   - src/backend/metagen/service.py §review_candidate:
 *     reject raises 409 ConflictError("METAGEN_CANNOT_REJECT_APPROVED", ...) when
 *     cand.status == "approved"; only "llm_approved" is reject-eligible.
 *   - src/backend/metagen/service.py §_emit_to_datahub (~L1218-1262):
 *     dataset.description  → EditableDatasetPropertiesClass  → aspect "editableDatasetProperties.description"
 *     column.<fp>.description → EditableSchemaMetadataClass[fieldPath] → aspect "editableSchemaMetadata"
 *
 * The aspect-name assertions in the destinationAspectLabel group are the primary
 * guard against regressing to a wrong name in the steward's approve confirm dialog
 * (the historical bug was showing "datasetProperties.description" instead of
 * "editableDatasetProperties.description").
 */

import { describe, it, expect } from "vitest";
import {
  isRejectEligible,
  isItemFinalized,
  findApprovedCandidate,
  destinationAspectLabel,
} from "./metagen-predicates";
import type { CandidateStatus, ItemStatus } from "@/types/metagen";

// ---------------------------------------------------------------------------
// Minimal factory helpers — fields not relevant to predicate logic are filled
// with safe defaults so tests remain readable and spec-focused.
// ---------------------------------------------------------------------------

function makeCandidate(status: CandidateStatus) {
  return {
    candidate_id: "cand-" + status,
    item_id: "item-1",
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    value: "A description of the catalog.title_master table",
    confidence_score: 0.92,
    status,
    evidence: {},
    created_at: "2026-05-01T00:00:00Z",
    reviewed_at: null,
    reviewer_id: null,
  };
}

function makeItemSummary(status: ItemStatus) {
  return {
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    item_id: "dataset.description",
    kind: "dataset.description" as const,
    field_path: null,
    status,
    candidate_count: 2,
    composite_id: "cid-1",
  };
}

// ---------------------------------------------------------------------------
// 1. isRejectEligible — mirrors backend 409 precondition
// ---------------------------------------------------------------------------
//
// Backend: reject raises ConflictError("METAGEN_CANNOT_REJECT_APPROVED") when
// cand.status == "approved". Only "llm_approved" is reject-eligible.
// (src/backend/metagen/service.py §review_candidate L770-775)

describe('isRejectEligible — reject-eligibility mirrors backend METAGEN_CANNOT_REJECT_APPROVED precondition (service.py §review_candidate)', () => {
  // Drive from the exhaustive candidate status table from src/api/schemas/metagen.py
  type Row = { status: CandidateStatus; expected: boolean; reason: string };

  const table: Row[] = [
    {
      status: "llm_approved",
      expected: true,
      reason: "llm_approved is the normal case — backend accepts reject",
    },
    {
      status: "approved",
      expected: false,
      reason: "backend raises 409 METAGEN_CANNOT_REJECT_APPROVED for approved candidates",
    },
    {
      status: "rejected",
      expected: false,
      reason: "re-rejecting a rejected candidate is meaningless",
    },
  ];

  table.forEach(({ status, expected, reason }) => {
    it(`status="${status}" → isRejectEligible=${expected} (${reason})`, () => {
      expect(isRejectEligible(makeCandidate(status))).toBe(expected);
    });
  });

  it("only 'llm_approved' returns true — exactly one status is reject-eligible", () => {
    const allStatuses: CandidateStatus[] = ["llm_approved", "approved", "rejected"];
    const eligibleStatuses = allStatuses.filter((s) => isRejectEligible(makeCandidate(s)));
    expect(eligibleStatuses).toEqual(["llm_approved"]);
  });

  it("default-deny for unknown/future status — isRejectEligible must not return true for unknown inputs", () => {
    // Guard against a future status being silently granted reject eligibility.
    const unknown = isRejectEligible({ status: "some_unknown_status" as CandidateStatus });
    expect(unknown).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. isItemFinalized — item collapses when status === "approved"
// ---------------------------------------------------------------------------
//
// spec/feature/FRONTEND_METAGEN.md: "Finalized items collapse to a single approved row"
// Item statuses from src/api/schemas/metagen.py MetagenItemSummary.status:
//   "pending" | "llm_approved" | "approved"

describe('isItemFinalized — item collapses to single approved row when status==="approved" (FRONTEND_METAGEN.md §Page contracts)', () => {
  type Row = { status: ItemStatus; expected: boolean };

  const table: Row[] = [
    { status: "approved",    expected: true  },
    { status: "pending",     expected: false },
    { status: "llm_approved", expected: false },
  ];

  table.forEach(({ status, expected }) => {
    it(`status="${status}" → isItemFinalized=${expected}`, () => {
      expect(isItemFinalized(makeItemSummary(status))).toBe(expected);
    });
  });

  it("returns false for all non-approved item statuses", () => {
    const nonApproved: ItemStatus[] = ["pending", "llm_approved"];
    nonApproved.forEach((s) => {
      expect(isItemFinalized(makeItemSummary(s))).toBe(false);
    });
  });

  it("returns true only for 'approved' — the single finalized state", () => {
    const allStatuses: ItemStatus[] = ["pending", "llm_approved", "approved"];
    const finalizedStatuses = allStatuses.filter((s) => isItemFinalized(makeItemSummary(s)));
    expect(finalizedStatuses).toEqual(["approved"]);
  });

  it("accepts a partial object with only status (type Pick<MetagenItemSummary, 'status'>)", () => {
    expect(isItemFinalized({ status: "approved" })).toBe(true);
    expect(isItemFinalized({ status: "pending" })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 3. findApprovedCandidate — returns the approved candidate or null
// ---------------------------------------------------------------------------
//
// spec/feature/FRONTEND_METAGEN.md: approved candidate shown as finalized row;
// null when no approved candidate yet (item still in review queue).

describe('findApprovedCandidate — returns approved candidate or null (FRONTEND_METAGEN.md §Per-dataset page)', () => {
  it("returns null for an empty candidates array (no throw)", () => {
    expect(findApprovedCandidate([])).toBeNull();
  });

  it("returns null when no candidate has status 'approved'", () => {
    const candidates = [
      makeCandidate("llm_approved"),
      makeCandidate("rejected"),
    ];
    expect(findApprovedCandidate(candidates)).toBeNull();
  });

  it("returns the approved candidate when present", () => {
    const approved = makeCandidate("approved");
    const candidates = [makeCandidate("llm_approved"), approved, makeCandidate("rejected")];
    const result = findApprovedCandidate(candidates);
    expect(result).not.toBeNull();
    expect(result!.status).toBe("approved");
    expect(result!.candidate_id).toBe("cand-approved");
  });

  it("returns the first approved candidate when only one is approved (backend partial-unique index)", () => {
    // Backend: UNIQUE (dataset_urn, item_id) WHERE status='approved' — at most one approved
    // per item at any given moment. The UI must handle whichever one is approved.
    const candidates = [
      { ...makeCandidate("llm_approved"), candidate_id: "c1" },
      { ...makeCandidate("approved"),     candidate_id: "c2" },
    ];
    const result = findApprovedCandidate(candidates);
    expect(result!.candidate_id).toBe("c2");
  });

  it("returns a candidate object (not just status) with all fields accessible", () => {
    const approved = { ...makeCandidate("approved"), value: "Catalog of Imazon book titles" };
    const result = findApprovedCandidate([makeCandidate("llm_approved"), approved]);
    expect(result!.value).toBe("Catalog of Imazon book titles");
    expect(result!.confidence_score).toBe(0.92);
  });

  it("returns null for a list of all llm_approved candidates (none human-approved yet)", () => {
    const candidates = [
      { ...makeCandidate("llm_approved"), candidate_id: "c1" },
      { ...makeCandidate("llm_approved"), candidate_id: "c2" },
      { ...makeCandidate("llm_approved"), candidate_id: "c3" },
    ];
    expect(findApprovedCandidate(candidates)).toBeNull();
  });

  it("returns null for a single rejected candidate", () => {
    expect(findApprovedCandidate([makeCandidate("rejected")])).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. destinationAspectLabel — aspect name table (critical correctness guard)
// ---------------------------------------------------------------------------
//
// This section is the primary regression guard for the aspect-name bug.
// The backend emits to EDITABLE aspects — not the read-only "datasetProperties"
// or "schemaMetadata" aspects. The confirm dialog must show the correct aspect.
//
// Backend authoritative mapping (src/backend/metagen/service.py §_emit_to_datahub):
//
//   if item_id == "dataset.description":
//       EditableDatasetPropertiesClass(description=value)
//         → aspect name: editableDatasetProperties.description
//
//   elif item_id.startswith("column.") and item_id.endswith(".description"):
//       EditableSchemaMetadataClass(editableSchemaFieldInfo=[...])
//         → aspect name: editableSchemaMetadata (with field path)
//
// WRONG (historical bug): "datasetProperties.description"
// CORRECT: "editableDatasetProperties.description"

describe('destinationAspectLabel — aspect names derived from service.py §_emit_to_datahub (~L1218-1262)', () => {
  describe('kind="dataset.description" → editableDatasetProperties.description (NOT datasetProperties.description)', () => {
    it('returns "editableDatasetProperties.description" for dataset.description (editable aspect, not read-only)', () => {
      // Backend: EditableDatasetPropertiesClass — editable aspect, not DatasetPropertiesClass
      // Bug guard: any result containing "datasetProperties" but NOT "editable" is wrong.
      const label = destinationAspectLabel("dataset.description", null);
      expect(label).toBe("editableDatasetProperties.description");
    });

    it('does NOT return "datasetProperties.description" (read-only aspect is not the emission target)', () => {
      const label = destinationAspectLabel("dataset.description", null);
      // Must not be the non-editable aspect (regression guard for the historical bug)
      expect(label).not.toBe("datasetProperties.description");
    });

    it('starts with "editable" — confirms the editable aspect prefix', () => {
      const label = destinationAspectLabel("dataset.description", null);
      expect(label.startsWith("editable")).toBe(true);
    });

    it('contains "editableDatasetProperties" — correct class name from DataHub schema', () => {
      const label = destinationAspectLabel("dataset.description", null);
      expect(label).toContain("editableDatasetProperties");
    });

    it('fieldPath=null does not affect dataset.description output', () => {
      expect(destinationAspectLabel("dataset.description", null))
        .toBe("editableDatasetProperties.description");
    });

    it('fieldPath="" (empty string) does not affect dataset.description output', () => {
      expect(destinationAspectLabel("dataset.description", ""))
        .toBe("editableDatasetProperties.description");
    });
  });

  describe('kind="column.description" → editableSchemaMetadata with fieldPath (NOT schemaMetadata)', () => {
    it('contains "editableSchemaMetadata" for column.description (backend: EditableSchemaMetadataClass)', () => {
      // Backend: EditableSchemaMetadataClass — not SchemaMetadataClass (read-only)
      const label = destinationAspectLabel("column.description", "isbn");
      expect(label).toContain("editableSchemaMetadata");
    });

    it('does NOT contain "schemaMetadata" without "editable" prefix (read-only aspect guard)', () => {
      const label = destinationAspectLabel("column.description", "isbn");
      // "editableSchemaMetadata" is fine; bare "schemaMetadata" (without "editable") is not
      // We check by stripping the editable prefix and ensuring no bare occurrence remains
      const stripped = label.replace("editableSchemaMetadata", "");
      expect(stripped).not.toContain("schemaMetadata");
    });

    it('includes the fieldPath "isbn" in the label', () => {
      const label = destinationAspectLabel("column.description", "isbn");
      expect(label).toContain("isbn");
    });

    it('includes the fieldPath "title" in the label', () => {
      const label = destinationAspectLabel("column.description", "title");
      expect(label).toContain("title");
    });

    it('includes the fieldPath "publication_year" in the label (multi-word column)', () => {
      const label = destinationAspectLabel("column.description", "publication_year");
      expect(label).toContain("publication_year");
    });

    it('null fieldPath for column.description still contains "editableSchemaMetadata"', () => {
      const label = destinationAspectLabel("column.description", null);
      expect(label).toContain("editableSchemaMetadata");
    });

    it('empty fieldPath for column.description still contains "editableSchemaMetadata"', () => {
      const label = destinationAspectLabel("column.description", "");
      expect(label).toContain("editableSchemaMetadata");
    });

    it('fieldPath is embedded in the label, not discarded', () => {
      const withPath = destinationAspectLabel("column.description", "user_id");
      const withoutPath = destinationAspectLabel("column.description", null);
      // A label with a real fieldPath must differ from the no-path label
      expect(withPath).not.toBe(withoutPath);
      expect(withPath.length).toBeGreaterThan(withoutPath.length);
    });
  });

  describe('aspect label exhaustive table — all kind/fieldPath combinations (service.py §_emit_to_datahub)', () => {
    // Summary table encoding the full backend dispatch logic.
    // If service.py's _emit_to_datahub changes aspect names, these tests will fail
    // and surface the divergence before the confirm dialog shows a wrong label.

    type Row = {
      kind: string;
      fieldPath: string | null;
      mustContain: string;
      mustNotContain?: string;
    };

    const table: Row[] = [
      {
        kind: "dataset.description",
        fieldPath: null,
        mustContain: "editableDatasetProperties.description",
        mustNotContain: "schemaMetadata",
      },
      {
        kind: "column.description",
        fieldPath: "isbn",
        mustContain: "editableSchemaMetadata",
      },
      {
        kind: "column.description",
        fieldPath: "isbn",
        mustContain: "isbn",
      },
      {
        kind: "column.description",
        fieldPath: "book_id",
        mustContain: "editableSchemaMetadata",
      },
      {
        kind: "column.description",
        fieldPath: "book_id",
        mustContain: "book_id",
      },
    ];

    table.forEach(({ kind, fieldPath, mustContain, mustNotContain }) => {
      const desc = mustNotContain
        ? `kind="${kind}" fieldPath=${JSON.stringify(fieldPath)} → contains "${mustContain}", not "${mustNotContain}"`
        : `kind="${kind}" fieldPath=${JSON.stringify(fieldPath)} → contains "${mustContain}"`;

      it(desc, () => {
        const label = destinationAspectLabel(kind, fieldPath);
        expect(label).toContain(mustContain);
        if (mustNotContain) {
          expect(label).not.toContain(mustNotContain);
        }
      });
    });
  });

  describe('unknown kind — safe fallback (does not throw, does not return empty)', () => {
    it('returns a non-empty string for an unknown kind', () => {
      const label = destinationAspectLabel("unknown.kind", null);
      expect(typeof label).toBe("string");
      expect(label.length).toBeGreaterThan(0);
    });

    it('does not throw for an empty kind string', () => {
      expect(() => destinationAspectLabel("", null)).not.toThrow();
    });

    it('returns the kind itself as fallback when kind is not recognized', () => {
      // The implementation returns `kind` for unknown kinds — safe fallback is not empty.
      const label = destinationAspectLabel("some.future.kind", null);
      expect(label).toBe("some.future.kind");
    });
  });
});

// ---------------------------------------------------------------------------
// 5. Cross-predicate consistency: isRejectEligible + isItemFinalized alignment
// ---------------------------------------------------------------------------
//
// When a candidate is "approved", isRejectEligible must be false.
// When an item is "approved" (finalized), it holds an approved candidate.
// These invariants must be consistent across the two predicates.

describe('cross-predicate consistency — approved state is coherent across isRejectEligible and isItemFinalized', () => {
  it("approved candidate is not reject-eligible AND approved item is finalized", () => {
    const approvedCandidate = makeCandidate("approved");
    const approvedItem = makeItemSummary("approved");

    expect(isRejectEligible(approvedCandidate)).toBe(false);
    expect(isItemFinalized(approvedItem)).toBe(true);
  });

  it("llm_approved candidate is reject-eligible AND llm_approved item is not finalized", () => {
    const llmCandidate = makeCandidate("llm_approved");
    const llmItem = makeItemSummary("llm_approved");

    expect(isRejectEligible(llmCandidate)).toBe(true);
    expect(isItemFinalized(llmItem)).toBe(false);
  });

  it("findApprovedCandidate returning non-null corresponds to item status=approved in a finalized item", () => {
    const candidates = [makeCandidate("approved"), makeCandidate("llm_approved")];
    const approved = findApprovedCandidate(candidates);

    // If findApprovedCandidate found an approved candidate, isRejectEligible must be false for it
    expect(approved).not.toBeNull();
    expect(isRejectEligible(approved!)).toBe(false);
  });
});
