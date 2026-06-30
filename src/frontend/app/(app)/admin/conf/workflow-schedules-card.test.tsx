/**
 * Tests for app/(app)/admin/conf/workflow-schedules-card.tsx — the self-contained
 * "Workflow schedules" section on the Configurations page.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Workflow schedules:
 *       renders one checkbox per DAG group (5 groups, verbatim labels); checkbox
 *       reads "Enabled" → checked = unpaused; toggling fires
 *       PATCH /admin/dags/{group} with {paused: !checked}; mixed → indeterminate.
 *   - spec/API.md §/admin/dags: groups[] = {group, paused, mixed, dags[]}.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { DagGroup, DagGroupStatus, DagGroupsResponse } from "@/lib/api/types";

// jsdom lacks ResizeObserver (used by Radix UI)
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ── Mocks (hoisted) ───────────────────────────────────────────────────────────
const mockUseDagGroups = vi.fn();
const mockMutate = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useDagGroups: () => mockUseDagGroups(),
  useSetDagGroupPaused: () => ({ mutate: mockMutate, isPending: false, variables: undefined }),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {}
  return { ApiError };
});

import { WorkflowSchedulesCard } from "./workflow-schedules-card";

// ── Helpers ───────────────────────────────────────────────────────────────────
function status(
  group: DagGroup,
  paused: boolean,
  mixed = false,
): DagGroupStatus {
  return { group, paused, mixed, dags: [{ dag_id: `${group}-hourly`, paused }] };
}

function groupsResponse(groups: DagGroupStatus[]): DagGroupsResponse {
  return { resp_time: "2026-06-30T00:00:00Z", groups };
}

const ALL_ENABLED = groupsResponse([
  status("datahub_sync", false),
  status("auth_role_sync", false),
  status("ingestion_active", false),
  status("ontogen", false),
  status("metagen", false),
  status("metrics", false),
]);

function dagState(id: string): string | null {
  const el = document.getElementById(id);
  return el?.getAttribute("data-state") ?? null;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseDagGroups.mockReturnValue({ data: ALL_ENABLED, isLoading: false, isError: false });
});

// ── Tests ─────────────────────────────────────────────────────────────────────
describe("WorkflowSchedulesCard — renders six group toggles (FRONTEND_BASIC.md §Workflow schedules)", () => {
  it("renders all six labelled DAG-group checkboxes and no extra/missing group", () => {
    render(<WorkflowSchedulesCard />);
    // spec: API.md §/admin/dags + FRONTEND_BASIC §Workflow schedules — the DagGroup
    // set is exactly these six. Each label appears verbatim.
    expect(screen.getByText("DataHub hourly sync")).toBeTruthy();
    expect(screen.getByText("Auth role sync")).toBeTruthy();
    expect(screen.getByText("Active ingestion")).toBeTruthy();
    expect(screen.getByText("Ontology generation")).toBeTruthy();
    expect(screen.getByText("Metadata generation")).toBeTruthy();
    expect(screen.getByText("Metrics")).toBeTruthy();

    const boxes = (
      [
        "datahub_sync",
        "auth_role_sync",
        "ingestion_active",
        "ontogen",
        "metagen",
        "metrics",
      ] as DagGroup[]
    ).map((g) => document.getElementById(`dag-group-${g}`));
    expect(boxes.every((b) => b !== null)).toBe(true);

    // Exact-count guard: exactly six group checkboxes — a dropped (e.g. auth_role_sync)
    // or a phantom group would change this count and fail the test.
    expect(document.querySelectorAll('[id^="dag-group-"]').length).toBe(6);
  });

  it("renders an unpaused group as checked (checked = unpaused)", () => {
    render(<WorkflowSchedulesCard />);
    expect(dagState("dag-group-ontogen")).toBe("checked");
  });

  it("renders a paused group as unchecked", () => {
    mockUseDagGroups.mockReturnValue({
      data: groupsResponse([
        status("datahub_sync", false),
        status("ingestion_active", false),
        status("ontogen", true),
        status("metagen", false),
        status("metrics", false),
      ]),
      isLoading: false,
      isError: false,
    });
    render(<WorkflowSchedulesCard />);
    expect(dagState("dag-group-ontogen")).toBe("unchecked");
  });

  it("renders a mixed group as indeterminate", () => {
    mockUseDagGroups.mockReturnValue({
      data: groupsResponse([
        status("datahub_sync", false),
        status("ingestion_active", true, true),
        status("ontogen", false),
        status("metagen", false),
        status("metrics", false),
      ]),
      isLoading: false,
      isError: false,
    });
    render(<WorkflowSchedulesCard />);
    expect(dagState("dag-group-ingestion_active")).toBe("indeterminate");
  });
});

describe("WorkflowSchedulesCard — auth_role_sync group row (API.md §/admin/dags DagGroup)", () => {
  it("renders the 'Auth role sync' labelled checkbox", () => {
    render(<WorkflowSchedulesCard />);
    // spec: API.md §/admin/dags — DagGroup includes auth_role_sync.
    expect(screen.getByText("Auth role sync")).toBeTruthy();
    expect(document.getElementById("dag-group-auth_role_sync")).not.toBeNull();
  });

  it("renders the auth_role_sync group as checked when unpaused", () => {
    render(<WorkflowSchedulesCard />);
    expect(dagState("dag-group-auth_role_sync")).toBe("checked");
  });

  it("toggling the enabled auth_role_sync group sends {paused: true}", async () => {
    const user = userEvent.setup();
    render(<WorkflowSchedulesCard />);

    await user.click(document.getElementById("dag-group-auth_role_sync")!);

    await waitFor(() => expect(mockMutate).toHaveBeenCalled());
    const arg = mockMutate.mock.calls[0][0] as { group: DagGroup; paused: boolean };
    expect(arg.group).toBe("auth_role_sync");
    expect(arg.paused).toBe(true);
  });
});

describe("WorkflowSchedulesCard — toggle fires PATCH with inverted paused (FRONTEND_BASIC.md §Workflow schedules)", () => {
  it("toggling an enabled (checked) group sends {paused: true}", async () => {
    const user = userEvent.setup();
    render(<WorkflowSchedulesCard />);

    await user.click(document.getElementById("dag-group-metrics")!);

    await waitFor(() => expect(mockMutate).toHaveBeenCalled());
    const arg = mockMutate.mock.calls[0][0] as { group: DagGroup; paused: boolean };
    expect(arg.group).toBe("metrics");
    expect(arg.paused).toBe(true);
  });

  it("toggling a paused (unchecked) group sends {paused: false}", async () => {
    mockUseDagGroups.mockReturnValue({
      data: groupsResponse([
        status("datahub_sync", true),
        status("ingestion_active", false),
        status("ontogen", false),
        status("metagen", false),
        status("metrics", false),
      ]),
      isLoading: false,
      isError: false,
    });

    const user = userEvent.setup();
    render(<WorkflowSchedulesCard />);

    await user.click(document.getElementById("dag-group-datahub_sync")!);

    await waitFor(() => expect(mockMutate).toHaveBeenCalled());
    const arg = mockMutate.mock.calls[0][0] as { group: DagGroup; paused: boolean };
    expect(arg.group).toBe("datahub_sync");
    expect(arg.paused).toBe(false);
  });
});

describe("WorkflowSchedulesCard — loading and error states", () => {
  it("shows skeletons while loading and renders no checkboxes", () => {
    mockUseDagGroups.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<WorkflowSchedulesCard />);
    expect(document.getElementById("dag-group-metrics")).toBeNull();
  });

  it("shows an error message when the query fails", () => {
    mockUseDagGroups.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("boom"),
    });
    render(<WorkflowSchedulesCard />);
    expect(screen.getByText(/failed to load workflow schedules/i)).toBeTruthy();
  });
});
