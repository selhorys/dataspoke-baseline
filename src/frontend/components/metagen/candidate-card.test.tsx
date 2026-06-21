/**
 * Tests for CandidateCard — the producing-conf indicator on a metagen candidate.
 *
 * After a conf is deleted, its candidates are retained but orphaned (conf_name
 * becomes null). The card must render a muted "no conf" indicator in that state,
 * and the producing conf's name as a tag when it is present. This is the UI half
 * of the conf-delete-retains contract.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §review queue — each candidate shows its
 *   producing conf; orphaned (parentless) candidates surface a muted "no conf"
 *   indicator rather than hiding the missing conf.
 * Spec: spec/feature/BACKEND.md §Metadata Generation Service — deleting a conf
 *   retains candidates and nulls conf_id/conf_name (read paths are null-safe).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { CandidateCard } from "./candidate-card";
import type { MetagenCandidate } from "@/types/metagen";

// EvidenceLink reads browser-reachable Langfuse config; not under test here.
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => ({ langfuseUrl: null, langfuseProjectId: null }),
}));

function makeCandidate(overrides: Partial<MetagenCandidate> = {}): MetagenCandidate {
  return {
    candidate_id: "cand-1",
    conf_id: "conf-1",
    conf_name: "catalog-docs",
    run_id: null,
    item_id: "dataset.description",
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    value: "A generated dataset description.",
    confidence_score: 0.91,
    status: "llm_approved",
    evidence: {},
    created_at: "2026-01-01T00:00:00Z",
    reviewed_at: null,
    reviewer_id: null,
    ...overrides,
  };
}

function renderCard(candidate: MetagenCandidate) {
  return render(
    <CandidateCard
      candidate={candidate}
      itemKind="dataset.description"
      fieldPath={null}
      canWrite={false}
      onApprove={vi.fn()}
      onReject={vi.fn()}
      isReviewing={false}
    />,
  );
}

describe("CandidateCard — producing-conf indicator", () => {
  it("renders the producing conf name as a tag when conf_name is present", () => {
    renderCard(makeCandidate({ conf_name: "catalog-docs" }));

    expect(screen.getByText("catalog-docs")).toBeTruthy();
    // No orphan indicator when the conf is present.
    expect(screen.queryByText(/no conf/i)).toBeNull();
  });

  it("renders a muted 'no conf' indicator when conf_name is null (orphaned candidate)", () => {
    // An orphaned candidate keeps its status but has no producing conf.
    renderCard(makeCandidate({ conf_id: null, conf_name: null }));

    const indicator = screen.getByText(/no conf/i);
    expect(indicator).toBeTruthy();
    // Muted styling distinguishes the parentless state from a real conf tag.
    expect(indicator.className).toMatch(/text-muted-foreground/);
    // The card still renders its value — the candidate is retained, not hidden.
    expect(screen.getByText("A generated dataset description.")).toBeTruthy();
  });

  it.each(["llm_approved", "approved", "rejected"] as const)(
    "shows the 'no conf' indicator for an orphaned %s candidate (every status retained)",
    (status) => {
      renderCard(makeCandidate({ conf_id: null, conf_name: null, status }));

      expect(screen.getByText(/no conf/i)).toBeTruthy();
    },
  );
});
