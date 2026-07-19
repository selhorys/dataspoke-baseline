/**
 * Tests for ItemKindTable — the per-item-kind candidate table on the
 * /data/[urn] MetaGen panel.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Per-dataset — rows are candidates;
 * the column.description table carries a leading field_path column and groups
 * rows by column (item), and each row's Approve / Reject is keyed to that row's
 * (dataset_urn, item_id, candidate_id).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { ItemKindTable } from "./item-kind-table";
import type {
  MetagenCandidate,
  MetagenItemDetail,
  MetagenItemSummary,
} from "@/types/metagen";

// Shared link components query peripheral-links; this suite mounts no
// QueryClientProvider. See lib/api/peripheral-links.mock.ts.
vi.mock("@/lib/api/peripheral-links", async () =>
  (await import("@/lib/api/peripheral-links.mock")).envOnlyPeripheralLinksModule(),
);


const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({ useToast: () => ({ toast: mockToast }) }));

// Per-item detail keyed by item_id; the review mutation captures its vars.
const detailByItem: Record<string, MetagenItemDetail> = {};
const reviewMutate = vi.fn();

vi.mock("@/lib/api/metagen", () => ({
  useMetagenItem: (_datasetUrn: string, itemId: string) => ({
    data: detailByItem[itemId],
    isLoading: false,
  }),
  useReviewCandidate: () => ({ mutate: reviewMutate, isPending: false }),
}));

const DATASET_URN = "urn:li:dataset:a";

function makeItem(
  itemId: string,
  kind: MetagenItemSummary["kind"],
  fieldPath: string | null,
): MetagenItemSummary {
  return {
    dataset_urn: DATASET_URN,
    item_id: itemId,
    kind,
    field_path: fieldPath,
    status: "llm_approved",
    candidate_count: 0,
    composite_id: `${DATASET_URN}::${itemId}`,
    created_at: "2026-05-01T12:00:00Z",
  };
}

function makeCandidate(
  candidateId: string,
  itemId: string,
  overrides: Partial<MetagenCandidate> = {},
): MetagenCandidate {
  return {
    candidate_id: candidateId,
    conf_id: "c1",
    conf_name: "catalog policy",
    run_id: null,
    item_id: itemId,
    dataset_urn: DATASET_URN,
    value: `value-${candidateId}`,
    confidence_score: 0.91,
    status: "llm_approved",
    evidence: {},
    created_at: "2026-05-01T12:00:00Z",
    reviewed_at: null,
    reviewer_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockToast.mockReset();
  reviewMutate.mockReset();
  for (const k of Object.keys(detailByItem)) delete detailByItem[k];
});

describe("ItemKindTable — dataset.description", () => {
  it("renders candidate rows with no field column", () => {
    const item = makeItem("dataset.description", "dataset.description", null);
    detailByItem["dataset.description"] = {
      ...item,
      candidates: [makeCandidate("cand-1", "dataset.description")],
    };
    render(
      <ItemKindTable items={[item]} groupByColumn={false} canWrite={true} />,
    );
    expect(screen.queryByText("field")).toBeNull();
    expect(screen.getByText("value-cand-1")).toBeTruthy();
  });

  it("renders the muted 'no conf' indicator for an orphaned candidate (conf_name null)", () => {
    // Spec: spec/feature/FRONTEND_METAGEN.md §Per-dataset — the "run info" column
    // shows the producing conf_name, "muted 'no conf' when null because the conf
    // was deleted". A candidate whose conf was deleted has conf_name === null and
    // renders the muted "no conf" badge instead of a conf-name badge.
    const item = makeItem("dataset.description", "dataset.description", null);
    detailByItem["dataset.description"] = {
      ...item,
      candidates: [
        makeCandidate("cand-orphan", "dataset.description", {
          conf_id: null,
          conf_name: null,
        }),
      ],
    };
    render(
      <ItemKindTable items={[item]} groupByColumn={false} canWrite={true} />,
    );
    // Muted "no conf" indicator renders for the orphaned candidate.
    const noConf = screen.getByText("no conf");
    expect(noConf).toBeTruthy();
    expect(noConf.className).toContain("text-muted-foreground");
    // No conf-name badge — the default makeCandidate name ("catalog policy")
    // must NOT appear, confirming the null branch (not the name branch) rendered.
    expect(screen.queryByText("catalog policy")).toBeNull();
  });

  it("keys the Approve action to the row's (datasetUrn, itemId, candidateId)", () => {
    const item = makeItem("dataset.description", "dataset.description", null);
    detailByItem["dataset.description"] = {
      ...item,
      candidates: [makeCandidate("cand-1", "dataset.description")],
    };
    render(
      <ItemKindTable items={[item]} groupByColumn={false} canWrite={true} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    // Confirm dialog → Approve
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(reviewMutate).toHaveBeenCalledTimes(1);
    expect(reviewMutate.mock.calls[0][0]).toMatchObject({
      datasetUrn: DATASET_URN,
      itemId: "dataset.description",
      candidateId: "cand-1",
      body: { verdict: "approve" },
    });
  });
});

describe("ItemKindTable — column.description (grouped by column)", () => {
  it("renders the leading field column and groups candidates per column", () => {
    const bookId = makeItem("column.description::book_id", "column.description", "book_id");
    const title = makeItem("column.description::title", "column.description", "title");
    detailByItem["column.description::book_id"] = {
      ...bookId,
      candidates: [
        makeCandidate("c-a", "column.description::book_id"),
        makeCandidate("c-b", "column.description::book_id"),
      ],
    };
    detailByItem["column.description::title"] = {
      ...title,
      candidates: [makeCandidate("c-c", "column.description::title")],
    };
    render(
      <ItemKindTable items={[bookId, title]} groupByColumn={true} canWrite={true} />,
    );

    expect(screen.getByText("field")).toBeTruthy();
    // field_path cells rendered once per column group
    expect(screen.getAllByText("book_id")).toHaveLength(1);
    expect(screen.getAllByText("title")).toHaveLength(1);
    // both of book_id's candidates render
    expect(screen.getByText("value-c-a")).toBeTruthy();
    expect(screen.getByText("value-c-b")).toBeTruthy();
    expect(screen.getByText("value-c-c")).toBeTruthy();
  });

  it("keys the Reject action of a grouped row to that row's column item", () => {
    const title = makeItem("column.description::title", "column.description", "title");
    detailByItem["column.description::title"] = {
      ...title,
      candidates: [
        makeCandidate("c-c", "column.description::title", { status: "approved" }),
      ],
    };
    render(
      <ItemKindTable items={[title]} groupByColumn={true} canWrite={true} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(reviewMutate.mock.calls[0][0]).toMatchObject({
      itemId: "column.description::title",
      candidateId: "c-c",
      body: { verdict: "reject" },
    });
  });

  it("hides action buttons for readers", () => {
    const item = makeItem("column.description::title", "column.description", "title");
    detailByItem["column.description::title"] = {
      ...item,
      candidates: [makeCandidate("c-c", "column.description::title")],
    };
    render(
      <ItemKindTable items={[item]} groupByColumn={true} canWrite={false} />,
    );
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    // within() import retained for table-scoped assertions if needed
    expect(within(document.body).getByText("value-c-c")).toBeTruthy();
  });
});
