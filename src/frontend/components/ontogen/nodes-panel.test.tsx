/**
 * Tests for the redesigned ontogen NodesPanel (representative of the uniform
 * Node / Edge / Triple result tables).
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md §Result table (6-column compact layout):
 *     Title, Description, Status, Confidence (+ Evidence button → modal),
 *     Actions (Approve/Reject → reason confirm), Created At.
 *   - spec/API.md §UC3 result rows: ?sort=created_at_asc|_desc (default desc),
 *     offset/limit pagination via the shared <Pagination> control.
 *   - Plan Workstream B: evidence as modal; reason entered in the confirm popup;
 *     review submits { verdict, reason }; sort + pagination change query params.
 *
 * Strategy: mock @/lib/api/ontogen (data + review + item-attr hooks), and stub
 * the Radix Dialog/Select with simple open-aware renderers so assertions stay on
 * behavior rather than jsdom portal/focus-trap mechanics. The data hook is a spy
 * so we can read the {offset, limit, sort} params the panel threads on each render.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import React from "react";
import type { OntogenNode } from "@/types/ontogen";

// ── hoisted spies ──────────────────────────────────────────────────────────
const {
  useNodesSpy,
  reviewMutateSpy,
  useItemAttrSpy,
} = vi.hoisted(() => ({
  useNodesSpy: vi.fn(),
  reviewMutateSpy: vi.fn(),
  useItemAttrSpy: vi.fn(),
}));

vi.mock("@/lib/api/ontogen", () => ({
  useOntogenNodes: (params: unknown) => useNodesSpy(params),
  useOntogenItemAttr: (kind: string, id: string, enabled: boolean) =>
    useItemAttrSpy(kind, id, enabled),
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

import { NodesPanel } from "./nodes-panel";

// ── fixtures ────────────────────────────────────────────────────────────────
function makeNode(overrides: Partial<OntogenNode> = {}): OntogenNode {
  return {
    id: "node-1",
    name: "Book",
    description: "A published title",
    confidence_score: 0.87,
    status: "llm_pending",
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

beforeEach(() => {
  vi.clearAllMocks();
  useItemAttrSpy.mockReturnValue({ data: { evidence: {} }, isLoading: false, error: null });
});

// ---------------------------------------------------------------------------
// 1. Uniform 6-column layout
// ---------------------------------------------------------------------------
describe("NodesPanel — 6-column compact layout", () => {
  it("renders the six standard column headers", () => {
    mockNodesResult([makeNode()]);
    render(<NodesPanel canWrite={false} />);
    for (const header of ["Title", "Description", "Status", "Confidence", "Actions", "Created At"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeTruthy();
    }
    expect(screen.getAllByRole("columnheader")).toHaveLength(6);
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
// 2. Evidence modal
// ---------------------------------------------------------------------------
describe("NodesPanel — evidence modal", () => {
  it("opens an Evidence dialog and lazily fetches the item attr on open", () => {
    mockNodesResult([makeNode({ id: "n-ev" })]);
    useItemAttrSpy.mockReturnValue({
      data: { evidence: { verdict: "approve", rounds: 2 } },
      isLoading: false,
      error: null,
    });
    render(<NodesPanel canWrite={false} />);

    // Closed initially: the attr hook is called with enabled=false.
    expect(useItemAttrSpy).toHaveBeenCalledWith("node", "n-ev", false);
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /evidence/i }));

    // Open: dialog visible and the attr hook now enabled (lazy fetch).
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(useItemAttrSpy).toHaveBeenCalledWith("node", "n-ev", true);
    // Evidence JSON is rendered.
    expect(screen.getByText(/"rounds": 2/)).toBeTruthy();
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
