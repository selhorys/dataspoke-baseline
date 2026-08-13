/**
 * Tests for app/(app)/metagen/conf/new/page.tsx — MetaGen create-conf page.
 *
 * The submit button lives in the top-right page header (external submit),
 * mirroring OntoGen: the header renders
 *   <Button type="submit" form="metagen-conf-form">Create conf</Button>
 * and MetagenConfForm renders a bare <form id="metagen-conf-form"> with NO
 * internal submit button. Editors see the create button; readers (canWrite=false)
 * get the ErrorState and no submit button.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_METAGEN.md §Conf create / detail — create page hosts
 *     the conf form; the create action is Editor/Admin only.
 *   - lib/api/metagen.ts useCreateMetagenConf → POST /spoke/metagen/conf.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// useMe — controllable per-test
const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

// create mutation — capture calls
const createMutate = vi.fn();
/** Mutable so a test can put a server error on the create mutation. */
let createError: unknown = null;
vi.mock("@/lib/api/metagen", () => ({
  useCreateMetagenConf: () => ({
    mutate: createMutate,
    isPending: false,
    error: createError,
  }),
}));

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// MetagenConfForm pulls in Radix Select / DatasetFilterEditor (ResizeObserver,
// not in jsdom). The create page exercises header gating + external submit, not
// the form internals. Stub it as a bare <form id={formId}> shell that calls
// onSubmit on native submit — so the header submit button (form={formId}) drives it.
vi.mock("@/components/metagen/conf-form", () => ({
  MetagenConfForm: ({
    formId,
    onSubmit,
    serverError,
    datasetFilterError,
  }: {
    formId: string;
    onSubmit: (body: Record<string, unknown>) => void;
    serverError?: string;
    datasetFilterError?: { message: string };
  }) =>
    React.createElement(
      "form",
      {
        id: formId,
        "data-testid": "conf-form",
        // The page decides which slot an error lands in; surface both to assert on.
        "data-server-error": serverError ?? "",
        "data-filter-error": datasetFilterError?.message ?? "",
        onSubmit: (e: React.FormEvent) => {
          e.preventDefault();
          onSubmit({
            name: "catalog policy",
            is_enabled: false,
            schedule_tier: null,
            dataset_filter: {},
            result_limit: 3,
            overwrite_pending: true,
          });
        },
      },
      // a stub input so the form has focusable content; no submit button inside.
      React.createElement("input", { "aria-label": "name", defaultValue: "catalog policy" }),
    ),
}));

// ---------------------------------------------------------------------------
// Import the page AFTER mocks are registered
// ---------------------------------------------------------------------------
import CreateMetagenConfPage from "./page";
import { ApiError } from "@/lib/api/client";

beforeEach(() => {
  vi.clearAllMocks();
  cleanup();
  createError = null;
});

describe("CreateMetagenConfPage — write gating (FRONTEND_METAGEN.md §Conf create)", () => {
  it("editor sees the header Create conf submit button bound to the conf form", () => {
    mockUseMe.mockReturnValue({ canWrite: true });

    render(<CreateMetagenConfPage />);

    // The form renders.
    expect(screen.getByTestId("conf-form")).toBeInTheDocument();

    // The header submit button is type=submit, wired to the form via form=.
    const createBtn = screen.getByRole("button", {
      name: /create conf/i,
    }) as HTMLButtonElement;
    expect(createBtn.type).toBe("submit");
    expect(createBtn.getAttribute("form")).toBe("metagen-conf-form");
  });

  it("reader (canWrite=false) sees the ErrorState and no submit button", () => {
    mockUseMe.mockReturnValue({ canWrite: false });

    render(<CreateMetagenConfPage />);

    // The role-gate ErrorState renders; the form and submit do not.
    expect(screen.getByText(/Editor role to create a conf/i)).toBeInTheDocument();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /create conf/i })).toBeNull();
  });
});

describe("CreateMetagenConfPage — submit invokes the create mutation", () => {
  it("submitting the form fires useCreateMetagenConf().mutate exactly once", async () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    const user = userEvent.setup();

    render(<CreateMetagenConfPage />);

    await user.click(screen.getByRole("button", { name: /create conf/i }));

    await waitFor(() => {
      expect(createMutate).toHaveBeenCalledTimes(1);
    });
    const body = createMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(body).toHaveProperty("name", "catalog policy");
    expect(body).toHaveProperty("result_limit", 3);
    expect(body).toHaveProperty("dataset_filter");
  });
});

describe("CreateMetagenConfPage — a create error lands in exactly one slot", () => {
  // spec/feature/FRONTEND_BASIC.md §Shared component notes: a 422
  // INVALID_DATASET_FILTER "renders inline against the field". The generic slot
  // is wired to the conf `name` Field, so the filter error must not go there too.
  it("routes a 422 INVALID_DATASET_FILTER to the editor only", () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    createError = new ApiError(
      {
        error_code: "INVALID_DATASET_FILTER",
        message: "unexpected token (at character 17)",
        trace_id: "t-1",
        resp_time: "2026-01-01T00:00:00Z",
        detail: { position: 17 },
      },
      422,
    );

    render(<CreateMetagenConfPage />);

    const form = screen.getByTestId("conf-form");
    expect(form.getAttribute("data-filter-error")).toContain("INVALID_DATASET_FILTER");
    expect(form.getAttribute("data-server-error")).toBe("");
  });

  it("keeps every other create error in the generic slot", () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    createError = new ApiError(
      {
        error_code: "CONFLICT",
        message: "name already used",
        trace_id: "t-2",
        resp_time: "2026-01-01T00:00:00Z",
      },
      409,
    );

    render(<CreateMetagenConfPage />);

    const form = screen.getByTestId("conf-form");
    expect(form.getAttribute("data-server-error")).toContain("CONFLICT");
    expect(form.getAttribute("data-filter-error")).toBe("");
  });
});
