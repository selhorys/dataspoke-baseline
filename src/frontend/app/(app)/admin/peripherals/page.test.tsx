/**
 * Tests for app/(app)/admin/peripherals/page.tsx — Admin Peripherals page.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Admin Peripherals:
 *       two cards (DataHub, Langfuse), each its own form + Save button (per-card
 *       partial PATCH); non-secret fields (service_corpuser_urn, default_env,
 *       project_id, environment_tag) prefilled from GET and sent plain; secrets
 *       (token, secret_key) start blank, are blank-omitted from PATCH, never echo
 *       "********" back as a value; only changed fields are PATCHed; admin-gated.
 *   - spec/API.md §/admin/peripherals/datahub + /langfuse: the response/patch shapes.
 *
 * Mocked: useMe, the four admin hooks, toast, timezone — Vitest unit tier (no API).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import type { DatahubPeripheral, LangfusePeripheral } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Shared mock factories
// ---------------------------------------------------------------------------

function makeDatahub(overrides: Partial<DatahubPeripheral> = {}): DatahubPeripheral {
  return {
    resp_time: "2026-06-26T00:00:00Z",
    gms_url: "http://datahub-gms:8080",
    kafka_brokers: "kafka:9092",
    token: "********",
    service_corpuser_urn: "urn:li:corpuser:dataspoke",
    default_env: "DEV",
    is_configured: true,
    updated_at: "2026-06-26T10:00:00Z",
    ...overrides,
  };
}

function makeLangfuse(overrides: Partial<LangfusePeripheral> = {}): LangfusePeripheral {
  return {
    resp_time: "2026-06-26T00:00:00Z",
    host: "http://langfuse:3000",
    public_key: "pk-test",
    secret_key: "********",
    project_id: "imazon-metadata",
    environment_tag: "production",
    is_configured: true,
    updated_at: "2026-06-26T10:00:00Z",
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

const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

const mockUseDatahub = vi.fn();
const mockUseLangfuse = vi.fn();
const mockUpdateDatahub = vi.fn();
const mockUpdateLangfuse = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useDatahubPeripheral: () => mockUseDatahub(),
  useLangfusePeripheral: () => mockUseLangfuse(),
  useUpdateDatahubPeripheral: () => ({ mutateAsync: mockUpdateDatahub, isPending: false }),
  useUpdateLangfusePeripheral: () => ({ mutateAsync: mockUpdateLangfuse, isPending: false }),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock("@/lib/preferences/timezone", () => ({
  useDisplayTz: () => "UTC",
}));

// ApiError — mirror the real constructor signature (payload, status)
vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    error_code: string;
    trace_id: string;
    status: number;
    constructor(
      payload: { error_code: string; message: string; trace_id: string },
      status: number,
    ) {
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
// Import the page component + pure helpers AFTER mocks are registered
// ---------------------------------------------------------------------------
import AdminPeripheralsPage from "./page";
import {
  datahubSchema,
  datahubToFormDefaults,
  datahubBuildPatch,
  langfuseToFormDefaults,
  langfuseBuildPatch,
} from "./peripherals-form.schema";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function adminMe() {
  return {
    me: {
      id: "u1",
      email: "admin@example.com",
      name: "Admin",
      role: "Admin" as const,
      has_google: false,
      created_at: "",
      updated_at: "",
    },
    isAdmin: true,
    isEditor: false,
    canWrite: true,
    isLoading: false,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMeFn.mockReturnValue(adminMe());
  mockUseDatahub.mockReturnValue({ data: makeDatahub(), isLoading: false });
  mockUseLangfuse.mockReturnValue({ data: makeLangfuse(), isLoading: false });
  mockUpdateDatahub.mockResolvedValue(makeDatahub({ updated_at: "2026-06-26T12:00:00Z" }));
  mockUpdateLangfuse.mockResolvedValue(makeLangfuse({ updated_at: "2026-06-26T12:00:00Z" }));
});

// ---------------------------------------------------------------------------
// 1. Page populates fields from GET (non-secret plain; secrets blank)
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — populate from GET (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("renders both card headings + Save buttons", async () => {
    render(<AdminPeripheralsPage />);
    expect(await screen.findByText("DataHub")).toBeTruthy();
    expect(screen.getByText("Langfuse")).toBeTruthy();
    expect(screen.getByRole("button", { name: /save datahub/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /save langfuse/i })).toBeTruthy();
  });

  it("prefills non-secret DataHub fields (service_corpuser_urn, default_env) from GET", async () => {
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      const urn = document.getElementById("datahub_service_corpuser_urn") as HTMLInputElement;
      expect(urn.value).toBe("urn:li:corpuser:dataspoke");
    });
    const env = document.getElementById("datahub_default_env") as HTMLInputElement;
    expect(env.value).toBe("DEV");
    const gms = document.getElementById("datahub_gms_url") as HTMLInputElement;
    expect(gms.value).toBe("http://datahub-gms:8080");
  });

  it("prefills non-secret Langfuse fields (project_id, environment_tag) from GET", async () => {
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      const proj = document.getElementById("langfuse_project_id") as HTMLInputElement;
      expect(proj.value).toBe("imazon-metadata");
    });
    const envTag = document.getElementById("langfuse_environment_tag") as HTMLInputElement;
    expect(envTag.value).toBe("production");
  });

  it("secret inputs (token, secret_key) ALWAYS render blank — never echo the masked indicator", async () => {
    // spec: peripherals-form.schema.ts toFormDefaults — secret blanked; never echo "********".
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_token")).toBeTruthy();
    });
    const token = document.getElementById("datahub_token") as HTMLInputElement;
    const secretKey = document.getElementById("langfuse_secret_key") as HTMLInputElement;
    expect(token.value).toBe("");
    expect(secretKey.value).toBe("");
  });
});

// ---------------------------------------------------------------------------
// 2. Pure-helper validation + diff semantics (the real schema module)
// ---------------------------------------------------------------------------
describe("peripherals-form.schema — toFormDefaults blanks secrets, buildPatch diffs (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("datahubToFormDefaults drops the masked token, keeps non-secret fields", () => {
    const defaults = datahubToFormDefaults(makeDatahub());
    expect(defaults.token).toBe("");
    expect(defaults.service_corpuser_urn).toBe("urn:li:corpuser:dataspoke");
    expect(defaults.default_env).toBe("DEV");
  });

  it("langfuseToFormDefaults drops the masked secret_key, keeps non-secret fields", () => {
    const defaults = langfuseToFormDefaults(makeLangfuse());
    expect(defaults.secret_key).toBe("");
    expect(defaults.project_id).toBe("imazon-metadata");
    expect(defaults.environment_tag).toBe("production");
  });

  it("datahubBuildPatch sends ONLY the changed non-secret field; omits blank token", () => {
    const loaded = makeDatahub();
    const values = { ...datahubToFormDefaults(loaded), default_env: "PROD" };
    const patch = datahubBuildPatch(values, loaded);
    expect(patch).toEqual({ default_env: "PROD" });
    // blank token (left untouched) must NOT appear → leave-current semantics.
    expect(patch).not.toHaveProperty("token");
    expect(patch).not.toHaveProperty("service_corpuser_urn");
  });

  it("datahubBuildPatch includes the token ONLY when the user typed a value", () => {
    const loaded = makeDatahub();
    const values = { ...datahubToFormDefaults(loaded), token: "new-pat-token" };
    const patch = datahubBuildPatch(values, loaded);
    expect(patch).toEqual({ token: "new-pat-token" });
  });

  it("langfuseBuildPatch sends ONLY changed non-secret fields; omits blank secret_key", () => {
    const loaded = makeLangfuse();
    const values = {
      ...langfuseToFormDefaults(loaded),
      environment_tag: "staging",
    };
    const patch = langfuseBuildPatch(values, loaded);
    expect(patch).toEqual({ environment_tag: "staging" });
    expect(patch).not.toHaveProperty("secret_key");
  });

  it("buildPatch returns {} when nothing changed (no-op save)", () => {
    const dh = makeDatahub();
    expect(datahubBuildPatch(datahubToFormDefaults(dh), dh)).toEqual({});
    const lf = makeLangfuse();
    expect(langfuseBuildPatch(langfuseToFormDefaults(lf), lf)).toEqual({});
  });

  it("datahubSchema accepts the form-default shape (all string fields)", () => {
    expect(datahubSchema.safeParse(datahubToFormDefaults(makeDatahub())).success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3. Partial PATCH save round-trip (DataHub card)
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — DataHub partial PATCH save (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("editing default_env → Save DataHub → PATCHes ONLY default_env, omits token", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    const envInput = (await waitFor(() => {
      const el = document.getElementById("datahub_default_env") as HTMLInputElement | null;
      expect(el?.value).toBe("DEV");
      return el!;
    })) as HTMLInputElement;

    await user.clear(envInput);
    await user.type(envInput, "PROD");

    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockUpdateDatahub).toHaveBeenCalled();
    });
    const patchBody = mockUpdateDatahub.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toEqual({ default_env: "PROD" });
    // blank token must not be sent (leave-current); langfuse hook untouched.
    expect(patchBody).not.toHaveProperty("token");
    expect(mockUpdateLangfuse).not.toHaveBeenCalled();
  });

  it("typed token IS included in the DataHub PATCH alongside a changed field", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    await waitFor(() => {
      expect(document.getElementById("datahub_token")).toBeTruthy();
    });
    const tokenInput = document.getElementById("datahub_token") as HTMLInputElement;
    await user.type(tokenInput, "fresh-pat");

    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockUpdateDatahub).toHaveBeenCalled();
    });
    const patchBody = mockUpdateDatahub.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toHaveProperty("token", "fresh-pat");
  });

  it("Save with no changes does NOT call the mutation; shows a no-changes toast", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    await waitFor(() => {
      expect(document.getElementById("datahub_default_env")).toBeTruthy();
    });
    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    expect(mockUpdateDatahub).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 4. Partial PATCH save round-trip (Langfuse card) + success toast
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — Langfuse partial PATCH save (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("editing environment_tag → Save Langfuse → PATCHes ONLY environment_tag; success toast", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    const tagInput = (await waitFor(() => {
      const el = document.getElementById("langfuse_environment_tag") as HTMLInputElement | null;
      expect(el?.value).toBe("production");
      return el!;
    })) as HTMLInputElement;

    await user.clear(tagInput);
    await user.type(tagInput, "staging");

    await user.click(screen.getByRole("button", { name: /save langfuse/i }));

    await waitFor(() => {
      expect(mockUpdateLangfuse).toHaveBeenCalled();
    });
    const patchBody = mockUpdateLangfuse.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toEqual({ environment_tag: "staging" });
    expect(patchBody).not.toHaveProperty("secret_key");
    expect(mockUpdateDatahub).not.toHaveBeenCalled();

    // success toast fired
    await waitFor(() => {
      const titles = mockToast.mock.calls.map((c) => (c[0] as { title?: string })?.title);
      expect(titles.some((t) => /saved/i.test(t ?? ""))).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// 5. Admin gate
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — admin gate (FRONTEND_BASIC.md §Routing)", () => {
  it("non-admin sees a permission message, not the cards", async () => {
    mockUseMeFn.mockReturnValue({ ...adminMe(), isAdmin: false });
    render(<AdminPeripheralsPage />);
    expect(await screen.findByText(/do not have permission/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /save datahub/i })).toBeNull();
  });
});
