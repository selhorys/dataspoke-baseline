/**
 * Tests for app/(app)/admin/conf/page.tsx — Configurations admin page.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Configurations (/admin/conf):
 *       loads via GET /admin/conf; submits PATCH /admin/conf with ONLY changed fields;
 *       llm_api_key always blank on load (masked write-only); blank api_key omitted from
 *       PATCH; typed api_key included; *_reviewer_model cleared → sent as null;
 *       shows returned updated_at after save; toast on success; destructive toast on error.
 *   - spec/API.md §/admin/conf + src/api/schemas/admin.py RuntimeConfPatchRequest bounds:
 *       *_llm_max_iterations: 1–20; *_debate_max_turns: 2–10; *_rag_k & ontology rag *_k:
 *       0–20; metagen_confidence_threshold: 0.0–1.0; validation_score_n_intervals: ≥1.
 *   - Numbers sent as numbers, booleans as booleans (not strings) in the PATCH body.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import type { RuntimeConf } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Shared mock factories
// ---------------------------------------------------------------------------

/** Build a fully-populated RuntimeConf response for testing. */
function makeConf(overrides: Partial<RuntimeConf> = {}): RuntimeConf {
  return {
    resp_time: "2026-05-29T00:00:00Z",
    llm_provider: "gemini",
    llm_model: "gemini-2.5-flash",
    llm_api_key: "********",
    ontogen_llm_max_iterations: 3,
    ontogen_debate_max_turns: 4,
    ontogen_debate_rag_k: 5,
    ontogen_debate_reviewer_model: null,
    metagen_llm_max_iterations: 3,
    metagen_debate_max_turns: 4,
    metagen_debate_rag_k: 5,
    metagen_debate_reviewer_model: null,
    metagen_confidence_threshold: 0.7,
    metagen_ontology_rag_node_k: 5,
    metagen_ontology_rag_edge_k: 5,
    metagen_ontology_rag_triple_k: 5,
    validation_score_n_intervals: 3,
    stub_redis_client: true,
    stub_llm_client: true,
    stub_pgvector_manager: true,
    stub_notification_service: true,
    auth_datahub_corp_group: "dataspoke-users",
    updated_at: "2026-05-29T10:00:00Z",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Browser API stubs — jsdom lacks ResizeObserver (used by Radix UI)
// ---------------------------------------------------------------------------
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

// useMe — controllable per-test via mockUseMeFn
const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

// useRuntimeConf + useUpdateRuntimeConf — separate controllable mocks
const mockUseRuntimeConf = vi.fn();
const mockMutateAsync = vi.fn();
// WorkflowSchedulesCard (rendered by the page) also calls useDagGroups +
// useSetDagGroupPaused — stub both so the page renders without network.
const mockSetDagPaused = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useRuntimeConf: () => mockUseRuntimeConf(),
  useUpdateRuntimeConf: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
  }),
  useDagGroups: () => ({ data: { resp_time: "", groups: [] }, isLoading: false, isError: false }),
  useSetDagGroupPaused: () => ({ mutate: mockSetDagPaused, isPending: false, variables: undefined }),
}));

// toast — capture calls
const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

// next/navigation — no-op stubs
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/admin/conf",
}));

// next/link — pass-through anchor
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ApiError — mirror the real constructor signature (payload: ApiErrorPayload, status: number)
vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    error_code: string;
    trace_id: string;
    status: number;
    constructor(payload: { error_code: string; message: string; trace_id: string; resp_time?: string }, status: number) {
      super(payload.message);
      this.name = "ApiError";
      this.error_code = payload.error_code;
      this.trace_id = payload.trace_id;
      this.status = status;
    }
  }
  return { ApiError, apiFetch: vi.fn() };
});

// ---------------------------------------------------------------------------
// Import the page component AFTER mocks are registered
// ---------------------------------------------------------------------------
import AdminConfPage from "./page";

// ---------------------------------------------------------------------------
// Import pure helpers and schema from the real shared module (F1)
// ---------------------------------------------------------------------------
import { confSchema, toFormDefaults, buildPatch } from "./conf-form.schema";
import type { ConfFormValues } from "./conf-form.schema";
import { formatDateTime } from "@/lib/format-time";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function adminMe() {
  return { me: { id: "u1", email: "admin@example.com", name: "Admin", role: "Admin" as const, has_password: true, has_google: false, created_at: "", updated_at: "" }, isAdmin: true, isEditor: false, canWrite: true, isLoading: false };
}

/**
 * A fully valid ConfFormValues object used as the base for schema bounds tests (F1).
 * Each bounds test overrides exactly one field to isolate the field under test.
 * Derives from toFormDefaults(makeConf()) so bounds trace to the real schema contract.
 */
const validValues: ConfFormValues = toFormDefaults(makeConf());

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.clearAllMocks();
  // Default: admin user, conf loaded
  mockUseMeFn.mockReturnValue(adminMe());
  mockUseRuntimeConf.mockReturnValue({ data: makeConf(), isLoading: false });
  mockMutateAsync.mockResolvedValue(makeConf({ updated_at: "2026-05-29T12:00:00Z" }));
});

// ---------------------------------------------------------------------------
// 1. Page renders loaded values from GET /admin/conf
// ---------------------------------------------------------------------------
describe("AdminConfPage — renders loaded conf values (FRONTEND_BASIC.md §Configurations)", () => {
  it("renders the LLM provider field with the loaded value", async () => {
    render(<AdminConfPage />);

    // Wait for useEffect(reset) to apply loaded conf to the form
    await waitFor(() => {
      const input = screen.getByLabelText(/provider/i) as HTMLInputElement;
      expect(input.value).toBe("gemini");
    });
  });

  it("renders the LLM model field with the loaded value", async () => {
    render(<AdminConfPage />);

    await waitFor(() => {
      // The Model label has a required "*" asterisk child — use the id directly
      const input = document.getElementById("llm_model") as HTMLInputElement | null;
      expect(input?.value).toBe("gemini-2.5-flash");
    });
  });

  it("llm_api_key input is ALWAYS blank on load — never prefilled (masked write-only, FRONTEND_BASIC.md §Configurations)", async () => {
    // spec: GET returns "********" (masked); the form input must always render empty
    // because the key is write-only and must never be pre-populated.
    render(<AdminConfPage />);

    await waitFor(() => {
      // F2: hard assertion — no fallback branching
      const input = document.getElementById("llm_api_key") as HTMLInputElement | null;
      expect(input).not.toBeNull();
      expect(input!.value).toBe("");
    });
  });

  it("renders the auth_datahub_corp_group field with the loaded value", async () => {
    render(<AdminConfPage />);

    await waitFor(() => {
      const input = screen.getByLabelText(/datahub corp group/i) as HTMLInputElement;
      expect(input.value).toBe("dataspoke-users");
    });
  });

  it("renders the validation_score_n_intervals field with the loaded value", async () => {
    render(<AdminConfPage />);

    await waitFor(() => {
      const input = document.getElementById("validation_score_n_intervals") as HTMLInputElement | null;
      expect(input?.value).toBe("3");
    });
  });

  it("renders the stub checkboxes with loaded boolean checked state (F4)", async () => {
    // makeConf sets stub_redis_client: true — the rendered checkbox must reflect checked state
    render(<AdminConfPage />);

    await waitFor(() => {
      // Radix Checkbox: checked state expressed via aria-checked="true" or data-state="checked"
      const redisCheckbox = document.getElementById("stub_redis_client");
      expect(redisCheckbox).not.toBeNull();
      const isChecked =
        redisCheckbox!.getAttribute("data-state") === "checked" ||
        redisCheckbox!.getAttribute("aria-checked") === "true";
      expect(isChecked).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// 2. Client-side validation — Zod schema bounds via the real confSchema (F1)
// ---------------------------------------------------------------------------
// Tests import confSchema from ./conf-form.schema (the real production schema).
// Each test builds a full valid ConfFormValues and overrides only the field under test.
// Bounds derive from spec/API.md §/admin/conf → src/api/schemas/admin.py RuntimeConfPatchRequest.

describe("confSchema bounds — *_debate_max_turns: 2–10 (spec/API.md §/admin/conf, RuntimeConfPatchRequest ge=2 le=10)", () => {
  it("rejects ontogen_debate_max_turns = 1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_max_turns: 1 }).success).toBe(false);
  });

  it("accepts ontogen_debate_max_turns = 2 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_max_turns: 2 }).success).toBe(true);
  });

  it("accepts ontogen_debate_max_turns = 10 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_max_turns: 10 }).success).toBe(true);
  });

  it("rejects ontogen_debate_max_turns = 11 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_max_turns: 11 }).success).toBe(false);
  });

  it("rejects metagen_debate_max_turns = 1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_max_turns: 1 }).success).toBe(false);
  });

  it("accepts metagen_debate_max_turns = 2 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_max_turns: 2 }).success).toBe(true);
  });

  it("accepts metagen_debate_max_turns = 10 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_max_turns: 10 }).success).toBe(true);
  });

  it("rejects metagen_debate_max_turns = 11 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_max_turns: 11 }).success).toBe(false);
  });
});

describe("confSchema bounds — *_llm_max_iterations: 1–20 (spec/API.md §/admin/conf, RuntimeConfPatchRequest ge=1 le=20)", () => {
  it("rejects ontogen_llm_max_iterations = 0 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_llm_max_iterations: 0 }).success).toBe(false);
  });

  it("accepts ontogen_llm_max_iterations = 1 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_llm_max_iterations: 1 }).success).toBe(true);
  });

  it("accepts ontogen_llm_max_iterations = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_llm_max_iterations: 20 }).success).toBe(true);
  });

  it("rejects ontogen_llm_max_iterations = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_llm_max_iterations: 21 }).success).toBe(false);
  });

  it("rejects metagen_llm_max_iterations = 0 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_llm_max_iterations: 0 }).success).toBe(false);
  });

  it("accepts metagen_llm_max_iterations = 1 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_llm_max_iterations: 1 }).success).toBe(true);
  });

  it("accepts metagen_llm_max_iterations = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_llm_max_iterations: 20 }).success).toBe(true);
  });

  it("rejects metagen_llm_max_iterations = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_llm_max_iterations: 21 }).success).toBe(false);
  });
});

describe("confSchema bounds — validation_score_n_intervals: ≥1 (spec/API.md §/admin/conf, RuntimeConfPatchRequest ge=1)", () => {
  it("rejects validation_score_n_intervals = 0 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, validation_score_n_intervals: 0 }).success).toBe(false);
  });

  it("accepts validation_score_n_intervals = 1 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, validation_score_n_intervals: 1 }).success).toBe(true);
  });

  it("accepts validation_score_n_intervals = 100 (no upper bound in spec)", () => {
    expect(confSchema.safeParse({ ...validValues, validation_score_n_intervals: 100 }).success).toBe(true);
  });
});

describe("confSchema bounds — *_rag_k and metagen_ontology_rag_*_k: 0–20 (spec/API.md §/admin/conf, RuntimeConfPatchRequest ge=0 le=20)", () => {
  it("rejects ontogen_debate_rag_k = -1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_rag_k: -1 }).success).toBe(false);
  });

  it("accepts ontogen_debate_rag_k = 0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_rag_k: 0 }).success).toBe(true);
  });

  it("accepts ontogen_debate_rag_k = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_rag_k: 20 }).success).toBe(true);
  });

  it("rejects ontogen_debate_rag_k = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_rag_k: 21 }).success).toBe(false);
  });

  it("rejects metagen_debate_rag_k = -1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_rag_k: -1 }).success).toBe(false);
  });

  it("accepts metagen_debate_rag_k = 0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_rag_k: 0 }).success).toBe(true);
  });

  it("accepts metagen_debate_rag_k = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_rag_k: 20 }).success).toBe(true);
  });

  it("rejects metagen_debate_rag_k = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_rag_k: 21 }).success).toBe(false);
  });

  it("rejects metagen_ontology_rag_node_k = -1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_node_k: -1 }).success).toBe(false);
  });

  it("accepts metagen_ontology_rag_node_k = 0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_node_k: 0 }).success).toBe(true);
  });

  it("accepts metagen_ontology_rag_node_k = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_node_k: 20 }).success).toBe(true);
  });

  it("rejects metagen_ontology_rag_node_k = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_node_k: 21 }).success).toBe(false);
  });

  it("rejects metagen_ontology_rag_edge_k = -1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_edge_k: -1 }).success).toBe(false);
  });

  it("accepts metagen_ontology_rag_edge_k = 0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_edge_k: 0 }).success).toBe(true);
  });

  it("accepts metagen_ontology_rag_edge_k = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_edge_k: 20 }).success).toBe(true);
  });

  it("rejects metagen_ontology_rag_edge_k = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_edge_k: 21 }).success).toBe(false);
  });

  it("rejects metagen_ontology_rag_triple_k = -1 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_triple_k: -1 }).success).toBe(false);
  });

  it("accepts metagen_ontology_rag_triple_k = 0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_triple_k: 0 }).success).toBe(true);
  });

  it("accepts metagen_ontology_rag_triple_k = 20 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_triple_k: 20 }).success).toBe(true);
  });

  it("rejects metagen_ontology_rag_triple_k = 21 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_ontology_rag_triple_k: 21 }).success).toBe(false);
  });
});

describe("confSchema bounds — metagen_confidence_threshold: 0.0–1.0 (spec/API.md §/admin/conf, RuntimeConfPatchRequest ge=0.0 le=1.0)", () => {
  it("rejects metagen_confidence_threshold = -0.01 (min-1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_confidence_threshold: -0.01 }).success).toBe(false);
  });

  it("accepts metagen_confidence_threshold = 0.0 (lower boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_confidence_threshold: 0.0 }).success).toBe(true);
  });

  it("accepts metagen_confidence_threshold = 1.0 (upper boundary passes)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_confidence_threshold: 1.0 }).success).toBe(true);
  });

  it("rejects metagen_confidence_threshold = 1.01 (max+1 fails)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_confidence_threshold: 1.01 }).success).toBe(false);
  });

  it("accepts metagen_confidence_threshold = 0.7 (typical value)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_confidence_threshold: 0.7 }).success).toBe(true);
  });
});

describe("confSchema required fields — provider/model/corp_group required; *_reviewer_model empty allowed (spec/API.md §/admin/conf)", () => {
  it("rejects empty llm_provider (required field fails)", () => {
    expect(confSchema.safeParse({ ...validValues, llm_provider: "" }).success).toBe(false);
  });

  it("rejects empty llm_model (required field fails)", () => {
    expect(confSchema.safeParse({ ...validValues, llm_model: "" }).success).toBe(false);
  });

  it("rejects empty auth_datahub_corp_group (required field fails)", () => {
    expect(confSchema.safeParse({ ...validValues, auth_datahub_corp_group: "" }).success).toBe(false);
  });

  it("accepts empty ontogen_debate_reviewer_model (optional, blank means clear)", () => {
    expect(confSchema.safeParse({ ...validValues, ontogen_debate_reviewer_model: "" }).success).toBe(true);
  });

  it("accepts empty metagen_debate_reviewer_model (optional, blank means clear)", () => {
    expect(confSchema.safeParse({ ...validValues, metagen_debate_reviewer_model: "" }).success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3. buildPatch pure unit tests — diff invariants (F1 / new suite)
// ---------------------------------------------------------------------------
// buildPatch(values, loaded) is the highest-value invariant: it drives every PATCH
// payload. Tests here cover all branching paths without rendering the page.
// Spec: FRONTEND_BASIC.md §Configurations, "submits PATCH with ONLY changed fields".

describe("buildPatch — no-op when values equal loaded (FRONTEND_BASIC.md §Configurations)", () => {
  it("returns {} when form values match the loaded conf exactly", () => {
    const loaded = makeConf();
    const values = toFormDefaults(loaded);
    expect(buildPatch(values, loaded)).toEqual({});
  });
});

describe("buildPatch — single numeric field change (FRONTEND_BASIC.md §Configurations)", () => {
  it("returns {ontogen_llm_max_iterations: 10} when only that field changes", () => {
    const loaded = makeConf();
    const values = { ...toFormDefaults(loaded), ontogen_llm_max_iterations: 10 };
    const patch = buildPatch(values, loaded);
    expect(patch).toEqual({ ontogen_llm_max_iterations: 10 });
    expect(typeof patch.ontogen_llm_max_iterations).toBe("number");
  });

  it("returns {metagen_confidence_threshold: 0.5} when only that field changes", () => {
    const loaded = makeConf();
    const values = { ...toFormDefaults(loaded), metagen_confidence_threshold: 0.5 };
    const patch = buildPatch(values, loaded);
    expect(patch).toEqual({ metagen_confidence_threshold: 0.5 });
    expect(typeof patch.metagen_confidence_threshold).toBe("number");
  });

  it("returns {validation_score_n_intervals: 10} when only that field changes", () => {
    const loaded = makeConf();
    const values = { ...toFormDefaults(loaded), validation_score_n_intervals: 10 };
    const patch = buildPatch(values, loaded);
    expect(patch).toEqual({ validation_score_n_intervals: 10 });
  });
});

describe("buildPatch — single boolean stub field toggle (FRONTEND_BASIC.md §Configurations)", () => {
  it("returns {stub_redis_client: false} when toggled from true", () => {
    const loaded = makeConf({ stub_redis_client: true });
    const values = { ...toFormDefaults(loaded), stub_redis_client: false };
    const patch = buildPatch(values, loaded);
    expect(patch).toEqual({ stub_redis_client: false });
    expect(typeof patch.stub_redis_client).toBe("boolean");
  });

  it("returns {stub_llm_client: true} when toggled from false", () => {
    const loaded = makeConf({ stub_llm_client: false });
    const values = { ...toFormDefaults(loaded), stub_llm_client: true };
    const patch = buildPatch(values, loaded);
    expect(patch).toEqual({ stub_llm_client: true });
    expect(typeof patch.stub_llm_client).toBe("boolean");
  });

  it("does NOT include stub_pgvector_manager when it is unchanged", () => {
    const loaded = makeConf({ stub_pgvector_manager: true });
    const values = toFormDefaults(loaded); // identical
    expect(buildPatch(values, loaded)).not.toHaveProperty("stub_pgvector_manager");
  });
});

describe("buildPatch — llm_api_key semantics (FRONTEND_BASIC.md §Configurations)", () => {
  it("omits llm_api_key when value is blank (loaded was masked '********')", () => {
    const loaded = makeConf({ llm_api_key: "********" });
    // toFormDefaults always sets llm_api_key = "" regardless of loaded value
    const values = toFormDefaults(loaded); // llm_api_key = ""
    expect(buildPatch(values, loaded)).not.toHaveProperty("llm_api_key");
  });

  it("includes llm_api_key when user typed a non-empty value", () => {
    const loaded = makeConf({ llm_api_key: "********" });
    const values = { ...toFormDefaults(loaded), llm_api_key: "sk-new-secret-key" };
    const patch = buildPatch(values, loaded);
    expect(patch).toHaveProperty("llm_api_key", "sk-new-secret-key");
  });
});

describe("buildPatch — *_reviewer_model empty string → null (FRONTEND_BASIC.md §Configurations)", () => {
  it("sends ontogen_debate_reviewer_model as null when changed from a value to empty string", () => {
    const loaded = makeConf({ ontogen_debate_reviewer_model: "gemini-2.5-pro" });
    const values = { ...toFormDefaults(loaded), ontogen_debate_reviewer_model: "" };
    const patch = buildPatch(values, loaded);
    expect(patch).toHaveProperty("ontogen_debate_reviewer_model", null);
  });

  it("sends metagen_debate_reviewer_model as null when changed from a value to empty string", () => {
    const loaded = makeConf({ metagen_debate_reviewer_model: "gemini-2.5-pro" });
    const values = { ...toFormDefaults(loaded), metagen_debate_reviewer_model: "" };
    const patch = buildPatch(values, loaded);
    expect(patch).toHaveProperty("metagen_debate_reviewer_model", null);
  });

  it("does NOT include ontogen_debate_reviewer_model when it was already null and stays empty", () => {
    // loaded.ontogen_debate_reviewer_model = null → toFormDefaults maps to ""
    // values.ontogen_debate_reviewer_model = "" → same as loaded-mapped → no change
    const loaded = makeConf({ ontogen_debate_reviewer_model: null });
    const values = toFormDefaults(loaded); // ontogen_debate_reviewer_model = ""
    expect(buildPatch(values, loaded)).not.toHaveProperty("ontogen_debate_reviewer_model");
  });

  it("includes ontogen_debate_reviewer_model as the new value when set from empty", () => {
    const loaded = makeConf({ ontogen_debate_reviewer_model: null });
    const values = { ...toFormDefaults(loaded), ontogen_debate_reviewer_model: "gemini-2.5-pro" };
    const patch = buildPatch(values, loaded);
    expect(patch).toHaveProperty("ontogen_debate_reviewer_model", "gemini-2.5-pro");
  });
});

// ---------------------------------------------------------------------------
// 4. Changed-field PATCH diff — only changed keys are submitted
// ---------------------------------------------------------------------------
describe("AdminConfPage — PATCH diff: only changed fields sent (FRONTEND_BASIC.md §Configurations)", () => {
  it("PATCHes ONLY the changed field when a single numeric field is updated", async () => {
    // spec: submitting with only one changed field PATCHes ONLY that key; numbers as numbers
    const user = userEvent.setup();
    render(<AdminConfPage />);

    // Wait for form to be populated with loaded conf (ontogen_llm_max_iterations = 3)
    await waitFor(() => {
      const input = document.getElementById("ontogen_llm_max_iterations") as HTMLInputElement | null;
      expect(input?.value).toBe("3");
    });

    // Change ontogen_llm_max_iterations from 3 to 5
    const input = document.getElementById("ontogen_llm_max_iterations") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "5");

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;

    // Only the changed field
    expect(patchBody).toHaveProperty("ontogen_llm_max_iterations", 5);

    // Value is a number, not a string
    expect(typeof patchBody["ontogen_llm_max_iterations"]).toBe("number");

    // Other fields must NOT be present in the patch
    expect(patchBody).not.toHaveProperty("llm_provider");
    expect(patchBody).not.toHaveProperty("llm_model");
    expect(patchBody).not.toHaveProperty("validation_score_n_intervals");
    expect(patchBody).not.toHaveProperty("metagen_debate_max_turns");
  });

  it("PATCHes boolean fields as booleans (not strings)", async () => {
    // spec: booleans as booleans in the PATCH body
    // Load conf with stub_redis_client=false so toggling it to true produces a diff
    mockUseRuntimeConf.mockReturnValue({
      data: makeConf({ stub_redis_client: false }),
      isLoading: false,
    });
    mockMutateAsync.mockResolvedValue(makeConf({ stub_redis_client: true }));

    const user = userEvent.setup();
    render(<AdminConfPage />);

    // Wait for form to reset with the loaded conf
    await waitFor(() => {
      const checkbox = document.getElementById("stub_redis_client");
      expect(checkbox).toBeTruthy();
    });

    // Click the Redis stub checkbox to toggle it from false → true
    const redisLabel = screen.getByText(/redis client stub/i);
    await user.click(redisLabel);

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toHaveProperty("stub_redis_client");
    // Must be a boolean true, not a string "true"
    expect(typeof patchBody["stub_redis_client"]).toBe("boolean");
    expect(patchBody["stub_redis_client"]).toBe(true);

    // Other stub fields must NOT be present (they were not changed)
    expect(patchBody).not.toHaveProperty("stub_llm_client");
    expect(patchBody).not.toHaveProperty("stub_pgvector_manager");
    expect(patchBody).not.toHaveProperty("stub_notification_service");
  });
});

// ---------------------------------------------------------------------------
// 5. llm_api_key PATCH behaviour
// ---------------------------------------------------------------------------
describe("AdminConfPage — llm_api_key PATCH behaviour (FRONTEND_BASIC.md §Configurations)", () => {
  it("blank llm_api_key is OMITTED from the PATCH (leave-current semantics)", async () => {
    // spec: blank api_key omitted → leave unchanged; typed value included
    const user = userEvent.setup();
    render(<AdminConfPage />);

    // Change a different field so the form is dirty (otherwise no PATCH fires)
    await waitFor(() => {
      const providerInput = screen.getByLabelText(/provider/i) as HTMLInputElement;
      expect(providerInput.value).toBe("gemini");
    });

    const providerInput = screen.getByLabelText(/provider/i) as HTMLInputElement;
    await user.clear(providerInput);
    await user.type(providerInput, "openai");

    // Leave llm_api_key blank (default)
    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;

    // llm_api_key must NOT be in the payload when blank
    expect(patchBody).not.toHaveProperty("llm_api_key");
    // Changed provider must be present
    expect(patchBody).toHaveProperty("llm_provider", "openai");
  });

  it("typed llm_api_key IS included in the PATCH", async () => {
    // spec: a typed value is included in the patch
    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      expect(document.getElementById("llm_api_key")).toBeTruthy();
    });

    // Type a new API key
    const apiKeyInput = document.getElementById("llm_api_key") as HTMLInputElement;
    await user.type(apiKeyInput, "sk-new-api-key-xyz");

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;

    // Typed API key must appear in the patch
    expect(patchBody).toHaveProperty("llm_api_key", "sk-new-api-key-xyz");
  });
});

// ---------------------------------------------------------------------------
// 6. *_reviewer_model cleared → sent as null
// ---------------------------------------------------------------------------
describe("AdminConfPage — reviewer_model cleared sends null (FRONTEND_BASIC.md §Configurations)", () => {
  it("sends ontogen_debate_reviewer_model as null when field is cleared from a loaded value", async () => {
    // spec: *_reviewer_model empty string → null when changed
    // Load conf with a non-null reviewer model
    const confWithReviewer = makeConf({ ontogen_debate_reviewer_model: "gemini-2.5-pro" });
    mockUseRuntimeConf.mockReturnValue({ data: confWithReviewer, isLoading: false });
    mockMutateAsync.mockResolvedValue(makeConf({ ontogen_debate_reviewer_model: null }));

    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      const input = document.getElementById("ontogen_debate_reviewer_model") as HTMLInputElement | null;
      expect(input?.value).toBe("gemini-2.5-pro");
    });

    // Clear the reviewer model field
    const reviewerInput = document.getElementById("ontogen_debate_reviewer_model") as HTMLInputElement;
    await user.clear(reviewerInput);

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;

    // Cleared reviewer model must be sent as null (not empty string)
    expect(patchBody).toHaveProperty("ontogen_debate_reviewer_model", null);
  });

  it("sends metagen_debate_reviewer_model as null when cleared from a loaded value", async () => {
    const confWithReviewer = makeConf({ metagen_debate_reviewer_model: "gemini-2.5-pro" });
    mockUseRuntimeConf.mockReturnValue({ data: confWithReviewer, isLoading: false });
    mockMutateAsync.mockResolvedValue(makeConf({ metagen_debate_reviewer_model: null }));

    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      const input = document.getElementById("metagen_debate_reviewer_model") as HTMLInputElement | null;
      expect(input?.value).toBe("gemini-2.5-pro");
    });

    const reviewerInput = document.getElementById("metagen_debate_reviewer_model") as HTMLInputElement;
    await user.clear(reviewerInput);

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    const patchBody = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toHaveProperty("metagen_debate_reviewer_model", null);
  });
});

// ---------------------------------------------------------------------------
// 7. Success and error toasts
// ---------------------------------------------------------------------------
describe("AdminConfPage — save success and error toasts (FRONTEND_BASIC.md §Configurations)", () => {
  it("shows a success toast after a successful PATCH", async () => {
    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      expect(screen.getByLabelText(/provider/i)).toBeTruthy();
    });

    // Change a field to produce a dirty form
    const providerInput = screen.getByLabelText(/provider/i) as HTMLInputElement;
    await user.clear(providerInput);
    await user.type(providerInput, "openai");

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });

    const toastCall = mockToast.mock.calls[0][0] as { title?: string; variant?: string };
    // Success toast must NOT be destructive
    expect(toastCall.variant).not.toBe("destructive");
    expect(toastCall.title).toBeTruthy();
  });

  it("shows a destructive toast when mutateAsync throws ApiError", async () => {
    const { ApiError } = await import("@/lib/api/client");
    mockMutateAsync.mockRejectedValue(
      new ApiError(
        { error_code: "UNAUTHORIZED", message: "Access denied", trace_id: "t1", resp_time: "" },
        403,
      ),
    );

    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      expect(screen.getByLabelText(/provider/i)).toBeTruthy();
    });

    const providerInput = screen.getByLabelText(/provider/i) as HTMLInputElement;
    await user.clear(providerInput);
    await user.type(providerInput, "openai");

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });

    const toastCall = mockToast.mock.calls[0][0] as { variant?: string; title?: string; description?: string };
    expect(toastCall.variant).toBe("destructive");
    expect(toastCall.description).toBe("Access denied");
  });
});

// ---------------------------------------------------------------------------
// 8. updated_at footer rendered after successful save (F5)
// ---------------------------------------------------------------------------
describe("AdminConfPage — updated_at footer shown after save (FRONTEND_BASIC.md §Configurations)", () => {
  it("renders the 'Saved · updated …' footer with the mutation-returned timestamp after save", async () => {
    // spec: shows returned updated_at after save (page.tsx ~line 437-441, savedAt state)
    // Initial load: updated_at = "2026-05-29T10:00:00Z"
    // Mutation resolves with: updated_at = "2026-05-29T12:00:00Z" (distinct from load value)
    const savedTimestamp = "2026-05-29T12:00:00Z";
    mockMutateAsync.mockResolvedValue(makeConf({ updated_at: savedTimestamp }));

    const user = userEvent.setup();
    render(<AdminConfPage />);

    // Before save, footer must NOT be present (savedAt state is null initially)
    await waitFor(() => {
      expect(screen.getByLabelText(/provider/i)).toBeTruthy();
    });
    expect(screen.queryByText(/saved · updated/i)).toBeNull();

    // Dirty the form and save
    const providerInput = screen.getByLabelText(/provider/i) as HTMLInputElement;
    await user.clear(providerInput);
    await user.type(providerInput, "openai");

    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockMutateAsync).toHaveBeenCalled();
    });

    // After save, the "Saved · updated …" footer must be rendered with the saved timestamp
    await waitFor(() => {
      const footer = screen.getByText(/saved · updated/i);
      expect(footer).toBeTruthy();
      // The timestamp is rendered via the shared tz-aware formatDateTime
      // (default display tz "local") — verify the saved value is present.
      const expectedDate = formatDateTime(savedTimestamp);
      expect(footer.textContent).toContain(expectedDate);
    });

    // The initial updated_at ("10:00") must NOT appear in the footer
    // (footer shows the mutation result, not the initial load value)
    const footer = screen.getByText(/saved · updated/i);
    const initialDate = formatDateTime("2026-05-29T10:00:00Z");
    expect(footer.textContent).not.toBe(`Saved · updated ${initialDate}`);
  });
});

// ---------------------------------------------------------------------------
// 9. Non-admin user sees permission denied message (no form rendered)
// ---------------------------------------------------------------------------
describe("AdminConfPage — non-admin access (FRONTEND_BASIC.md §Routing)", () => {
  it("renders a permission-denied message for Reader role (no admin access)", async () => {
    // spec/feature/FRONTEND_BASIC.md §Routing: /admin/* is server-side gated by API role;
    // UI also suppresses the form when isAdmin is false.
    mockUseMeFn.mockReturnValue({
      me: { id: "u2", email: "reader@example.com", name: "Reader", role: "Reader" as const, has_password: true, has_google: false, created_at: "", updated_at: "" },
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });

    render(<AdminConfPage />);

    await waitFor(() => {
      // The page renders a permission error message, not the form
      expect(screen.queryByRole("button", { name: /save changes/i })).toBeNull();
      expect(screen.getByText(/do not have permission/i)).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------
// 10. No PATCH when form is not dirty
// ---------------------------------------------------------------------------
describe("AdminConfPage — no PATCH when no fields changed", () => {
  it("shows a toast without calling mutateAsync when no fields are changed", async () => {
    // spec: spec/feature/FRONTEND_BASIC.md §Configurations (`/admin/conf`) — the form
    // "saves edits with a partial `PATCH /admin/conf` (only changed fields)", so with
    // no changed fields there is nothing to send and no network call should be made.
    const user = userEvent.setup();
    render(<AdminConfPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /save changes/i })).toBeTruthy();
    });

    // Click save without changing anything
    const saveButton = screen.getByRole("button", { name: /save changes/i });
    await user.click(saveButton);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });

    // No PATCH should fire
    expect(mockMutateAsync).not.toHaveBeenCalled();

    const toastCall = mockToast.mock.calls[0][0] as { title?: string };
    expect(toastCall.title).toMatch(/no changes/i);
  });
});
