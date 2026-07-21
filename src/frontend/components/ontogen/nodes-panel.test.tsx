/**
 * Tests for the redesigned ontogen NodesPanel (representative of the uniform
 * Node / Edge / Triple result tables).
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md §Page contracts (7-column compact layout):
 *     Title, Description, Status, Confidence (score only), Actions
 *     (Approve/Reject → reason confirm), Created At, Evidence (Langfuse session
 *     Link, new tab).
 *   - spec/API.md §UC3 result rows: ?sort=created_at_asc|_desc (default desc),
 *     offset/limit pagination via the shared <Pagination> control.
 *   - spec/feature/FRONTEND_ONTOGEN.md §Page contracts (Evidence cell): the URL is
 *     built client-side as {langfuse_url}/project/{langfuse_project_id}/sessions/
 *     {run_id}, opens in a new tab, and "renders only when all three values are
 *     present; otherwise the cell shows `—`". Both the host and the project slug
 *     resolve from GET /spoke/common/peripheral-links.
 *   - spec/feature/FRONTEND_ONTOGEN.md §Page contracts (review): choosing an action
 *     opens a confirmation dialog carrying a free-text reason field; confirming
 *     posts method/review with {verdict, reason}.
 *
 * Strategy: mock @/lib/api/ontogen (data + review hooks), and stub the Radix
 * Dialog/Select with simple open-aware renderers so assertions stay on behavior
 * rather than jsdom portal/focus-trap mechanics. The data hook is a spy so we can
 * read the {offset, limit, sort} params the panel threads on each render. The
 * Langfuse host and project slug are supplied through a mocked useDisplayLinks,
 * the same way components/ontogen/evidence-link.test.tsx supplies them — this
 * suite is about the table, and the hook's own resolution from
 * GET /spoke/common/peripheral-links is covered in lib/api/peripheral-links.test.tsx.
 * One Evidence case keeps the real hook and seeds only the endpoint response, so
 * the wiring from the endpoint through the row to the href stays pinned here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
import React from "react";
import type { OntogenNode } from "@/types/ontogen";

// ── hoisted spies ──────────────────────────────────────────────────────────
const {
  useNodesSpy,
  reviewMutateSpy,
} = vi.hoisted(() => ({
  useNodesSpy: vi.fn(),
  reviewMutateSpy: vi.fn(),
}));

vi.mock("@/lib/api/ontogen", () => ({
  useOntogenNodes: (params: unknown) => useNodesSpy(params),
  useReviewOntogenItem: () => ({ mutate: reviewMutateSpy, isPending: false }),
}));

// Dialog stub: `Dialog` always renders its children (so the trigger Button,
// which the components nest inside <Dialog>, is present even when closed); only
// DialogContent is gated on `open` and rendered as role="dialog". `open` is
// propagated via a tiny context. This sidesteps Radix portal/focus mechanics in
// jsdom while keeping open/close behavior observable.
const DialogOpenCtx = React.createContext(false);

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    React.createElement(DialogOpenCtx.Provider, { value: !!open }, children),
  DialogContent: ({ children }: { children?: React.ReactNode }) => {
    const open = React.useContext(DialogOpenCtx);
    return open ? React.createElement("div", { role: "dialog" }, children) : null;
  },
  DialogHeader: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("div", null, children),
  DialogFooter: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("div", null, children),
  DialogTitle: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("h2", null, children),
}));

// Select stub (used by SortControl): statically-present clickable options.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children?: React.ReactNode;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "select-root", "data-value": value },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectTrigger: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("div", null, children),
  SelectValue: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("span", null, children),
  SelectContent: ({
    children,
    onValueChange,
  }: {
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "div",
      null,
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectItem: ({
    value,
    children,
    onValueChange,
  }: {
    value: string;
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "button",
      { "data-testid": `opt-${value}`, type: "button", onClick: () => onValueChange?.(value) },
      children,
    ),
}));

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));
vi.mock("@/components/ui/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

// The peripheral display links reach the row through useDisplayLinks. Most cases
// here drive that hook directly so the Evidence assertions describe the table, not
// the hook's own read (its resolution from GET /spoke/common/peripheral-links lives
// in lib/api/peripheral-links.test.tsx). One case flips `useRealDisplayLinks` and
// runs the panel over the genuine hook, so a row that stopped calling it — and read
// some other plane instead — fails here rather than only in E2E. The flag is set
// before a render and never toggled during a mount, so each mounted tree sees one
// consistent implementation.
const { mockUseDisplayLinks, useRealDisplayLinks, mockApiFetch } = vi.hoisted(() => ({
  mockUseDisplayLinks: vi.fn(),
  useRealDisplayLinks: { value: false },
  mockApiFetch: vi.fn(),
}));

vi.mock("@/lib/api/client", () => ({
  apiFetch: (path: string) => mockApiFetch(path),
}));

vi.mock("@/lib/api/peripheral-links", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/peripheral-links")>();
  return {
    ...actual,
    useDisplayLinks: () =>
      useRealDisplayLinks.value ? actual.useDisplayLinks() : mockUseDisplayLinks(),
  };
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NodesPanel } from "./nodes-panel";


// ── fixtures ────────────────────────────────────────────────────────────────
function makeNode(overrides: Partial<OntogenNode> = {}): OntogenNode {
  return {
    id: "node-1",
    name: "Book",
    description: "A published title",
    confidence_score: 0.87,
    status: "llm_pending",
    run_id: "11111111-1111-1111-1111-111111111111",
    created_at: "2026-01-02T03:04:05Z",
    updated_at: "2026-01-02T03:04:05Z",
    ...overrides,
  };
}

function mockNodesResult(nodes: OntogenNode[], total = nodes.length) {
  useNodesSpy.mockReturnValue({
    data: { nodes, total_count: total, offset: 0, limit: 20 },
    isLoading: false,
    error: null,
  });
}

/** Drives the mocked useDisplayLinks, mirroring evidence-link.test.tsx. */
function setLinks(langfuseUrl: string, langfuseProjectId: string): void {
  mockUseDisplayLinks.mockReturnValue({
    datahubUrl: "",
    langfuseUrl,
    langfuseProjectId,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  useRealDisplayLinks.value = false;
  // A wired Langfuse peripheral, so the Evidence link can be built by default.
  setLinks("http://langfuse.example.com", "dataspoke-project");
});

afterEach(() => {
  // Restore the stubbed hook for every subsequent case, whatever this one set.
  useRealDisplayLinks.value = false;
});

// ---------------------------------------------------------------------------
// 1. Uniform 7-column layout
// ---------------------------------------------------------------------------
describe("NodesPanel — 7-column compact layout", () => {
  it("renders the seven standard column headers", () => {
    mockNodesResult([makeNode()]);
    render(<NodesPanel canWrite={false} />);
    for (const header of [
      "Title",
      "Description",
      "Status",
      "Confidence",
      "Actions",
      "Created At",
      "Evidence",
    ]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeTruthy();
    }
    expect(screen.getAllByRole("columnheader")).toHaveLength(7);
  });

  it("renders the node name, description, and 2-dp confidence", () => {
    mockNodesResult([makeNode({ name: "Edition", description: "A format", confidence_score: 0.5 })]);
    render(<NodesPanel canWrite={false} />);
    expect(screen.getByText("Edition")).toBeTruthy();
    expect(screen.getByText("A format")).toBeTruthy();
    expect(screen.getByText("0.50")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 2. Evidence Langfuse-session link
// ---------------------------------------------------------------------------
describe("NodesPanel — evidence Langfuse-session link", () => {
  it("renders an external Link to the run's Langfuse session, opening a new tab", () => {
    mockNodesResult([
      makeNode({ id: "n-ev", run_id: "22222222-2222-2222-2222-222222222222" }),
    ]);
    render(<NodesPanel canWrite={false} />);

    const link = screen.getByRole("link", { name: /link/i });
    expect(link.getAttribute("href")).toBe(
      "http://langfuse.example.com/project/dataspoke-project/sessions/22222222-2222-2222-2222-222222222222",
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("renders — (no link) when the row has no run_id", () => {
    mockNodesResult([makeNode({ id: "n-seeded", run_id: null })]);
    render(<NodesPanel canWrite={false} />);

    expect(screen.queryByRole("link", { name: /link/i })).toBeNull();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("renders — (no link) when Langfuse is not wired as a peripheral", () => {
    // spec: FRONTEND_ONTOGEN.md §Page contracts — the Evidence Link needs the
    //   Langfuse host and project slug, both of which resolve from
    //   GET /spoke/common/peripheral-links; an unwired peripheral yields "".
    setLinks("", "");
    mockNodesResult([makeNode({ id: "n-unconfigured" })]);
    render(<NodesPanel canWrite={false} />);

    expect(screen.queryByRole("link", { name: /link/i })).toBeNull();
    // Backstop: the row rendered at all, so the absent link is the unwired-state
    // behavior rather than an empty table.
    expect(screen.getByText("Book")).toBeTruthy();
  });

  it("builds the Evidence href from GET /spoke/common/peripheral-links over the real hook", async () => {
    // spec: FRONTEND_ONTOGEN.md §Page contracts (Evidence cell) — "Both the host
    //   and the project slug resolve by the shared peripheral rule — from
    //   GET /spoke/common/peripheral-links".
    // spec: FRONTEND_BASIC.md §Shell — that endpoint is the sole source of
    //   langfuse_url / langfuse_project_id, "so nothing can mask what the DB
    //   holds".
    // The single case in this suite that runs the panel over the genuine
    // useDisplayLinks: only the endpoint response is seeded, so a row that read
    // any other plane cannot produce the href below.
    useRealDisplayLinks.value = true;
    mockApiFetch.mockResolvedValue({
      resp_time: "2026-07-19T00:00:00.000Z",
      datahub_url: "",
      langfuse_url: "https://langfuse.imazon.example.com",
      langfuse_project_id: "imazon-project",
    });
    mockNodesResult([
      makeNode({ id: "n-real", run_id: "33333333-3333-3333-3333-333333333333" }),
    ]);

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <NodesPanel canWrite={false} />
      </QueryClientProvider>,
    );

    // Backstop: the panel really did read the endpoint — the href below is the
    // response reaching the row, not a value the component carried.
    await waitFor(() =>
      expect(mockApiFetch).toHaveBeenCalledWith("/spoke/common/peripheral-links"),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: /link/i }).getAttribute("href"),
      ).toBe(
        "https://langfuse.imazon.example.com/project/imazon-project/sessions/33333333-3333-3333-3333-333333333333",
      ),
    );

    queryClient.clear();
  });
});

// ---------------------------------------------------------------------------
// 3. Approve → reason confirm fires { verdict, reason }
// ---------------------------------------------------------------------------
describe("NodesPanel — reason-confirm review", () => {
  it("has no inline reason input before the confirm dialog opens", () => {
    mockNodesResult([makeNode()]);
    render(<NodesPanel canWrite={true} />);
    // No textarea is rendered until Approve/Reject opens the confirm popup.
    expect(screen.queryByPlaceholderText(/reason/i)).toBeNull();
  });

  it("opens the confirm popup on Approve and submits { verdict, reason }", () => {
    mockNodesResult([makeNode({ id: "n-approve" })]);
    render(<NodesPanel canWrite={true} />);

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    const dialog = screen.getByRole("dialog");

    // Reason entered in the popup (not inline).
    const reason = within(dialog).getByPlaceholderText(/reason/i);
    fireEvent.change(reason, { target: { value: "looks correct" } });

    fireEvent.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(reviewMutateSpy).toHaveBeenCalledTimes(1);
    const [vars] = reviewMutateSpy.mock.calls[0];
    expect(vars).toMatchObject({
      kind: "node",
      id: "n-approve",
      body: { verdict: "approve", reason: "looks correct" },
    });
  });

  it("submits { verdict } with reason undefined when the reason is left blank", () => {
    // FRONTEND_ONTOGEN.md L69 — the reason field is free-text/optional; an empty
    // reason must be coerced to undefined (omitted), not sent as an empty string.
    mockNodesResult([makeNode({ id: "n-approve-blank" })]);
    render(<NodesPanel canWrite={true} />);

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    const dialog = screen.getByRole("dialog");

    // Confirm without touching the (optional) reason textarea.
    fireEvent.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(reviewMutateSpy).toHaveBeenCalledTimes(1);
    const [vars] = reviewMutateSpy.mock.calls[0];
    expect(vars).toMatchObject({
      kind: "node",
      id: "n-approve-blank",
      body: { verdict: "approve" },
    });
    expect((vars as { body: { reason?: string } }).body.reason).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 4. Sort + pagination thread into the query params
// ---------------------------------------------------------------------------
describe("NodesPanel — sort & pagination query params", () => {
  it("defaults the list query to created_at_desc, offset 0, default limit", () => {
    mockNodesResult([makeNode()]);
    render(<NodesPanel canWrite={false} />);
    const params = useNodesSpy.mock.calls[0][0] as { offset: number; limit: number; sort: string };
    expect(params.sort).toBe("created_at_desc");
    expect(params.offset).toBe(0);
    expect(params.limit).toBe(20);
  });

  it("re-queries with sort=created_at_asc when the sort control changes", () => {
    mockNodesResult([makeNode()]);
    render(<NodesPanel canWrite={false} />);

    fireEvent.click(screen.getByTestId("opt-created_at_asc"));

    const lastParams = useNodesSpy.mock.calls.at(-1)![0] as { sort: string; offset: number };
    expect(lastParams.sort).toBe("created_at_asc");
    // Changing sort resets to the first page.
    expect(lastParams.offset).toBe(0);
  });
});
