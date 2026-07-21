/**
 * Tests for lib/runtime-config.ts — getRuntimeConfig fallback precedence.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "Airflow, ReDoc | Deployment-local |
 *     Runtime config only (`airflowUrl`, `apiBaseUrl`)". Those two deployment-local
 *     values are the whole of the runtime config; the externally-wired peripherals
 *     (DataHub, Langfuse) resolve from `GET /spoke/common/peripheral-links` and are
 *     therefore covered in lib/api/peripheral-links.test.tsx, not here.
 *   - src/frontend/README.md §Production / runtime configuration — one image,
 *     configured at request time by server-side DATASPOKE_* env vars; the
 *     NEXT_PUBLIC_* vars remain a dev-only (`pnpm dev` + .env.local) fallback.
 *   - Resolution order: window.__DATASPOKE_RUNTIME_CONFIG__ > NEXT_PUBLIC_* > ""
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

// Import after any setup so module-level code runs in the right context.
// We re-import via vi.importActual to get a fresh module each describe block.

describe("getRuntimeConfig — fallback precedence", () => {
  beforeEach(() => {
    // Start each test without a global override.
    if (typeof window !== "undefined") {
      delete window.__DATASPOKE_RUNTIME_CONFIG__;
    }
    // Clear NEXT_PUBLIC_ vars by setting them to undefined.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", undefined as unknown as string);
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", undefined as unknown as string);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    if (typeof window !== "undefined") {
      delete window.__DATASPOKE_RUNTIME_CONFIG__;
    }
  });

  it("returns empty strings when neither window global nor NEXT_PUBLIC env vars are set", async () => {
    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();
    // spec: FRONTEND_BASIC.md §Shell — "Each icon renders only when its URL
    //   resolves non-empty"; an unconfigured deployment-local URL is "".
    expect(config.apiBaseUrl).toBe("");
    expect(config.airflowUrl).toBe("");
  });

  it("carries only the deployment-local fields", async () => {
    // spec: FRONTEND_BASIC.md §Shell resolution table — the runtime config is the
    // source for Airflow + ReDoc (`airflowUrl`, `apiBaseUrl`) and for nothing else;
    // DataHub / Langfuse resolve from GET /spoke/common/peripheral-links "only".
    // Asserting the exact key set is what makes "sole source" falsifiable here: a
    // reintroduced datahubUrl/langfuseUrl/langfuseProjectId field fails this case.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://airflow.example.com");

    const { getRuntimeConfig } = await import("./runtime-config");

    // Without the window global (the SSR / no-injection branch) …
    expect(Object.keys(getRuntimeConfig()).sort()).toEqual(["airflowUrl", "apiBaseUrl"]);

    // … and with it, so neither branch can grow a peripheral field.
    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
      airflowUrl: "http://runtime-airflow.example.com",
    };
    expect(Object.keys(getRuntimeConfig()).sort()).toEqual(["airflowUrl", "apiBaseUrl"]);
  });

  it("falls back to the NEXT_PUBLIC env vars when the window global is absent", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://airflow.example.com");

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://api.example.com");
    expect(config.airflowUrl).toBe("http://airflow.example.com");
  });

  it("prefers window.__DATASPOKE_RUNTIME_CONFIG__ over NEXT_PUBLIC env vars", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
      airflowUrl: "http://runtime-airflow.example.com",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // The request-time injection is what lets one image serve every deployment,
    // so it must beat the value inlined at build time.
    expect(config.apiBaseUrl).toBe("http://runtime.example.com");
    expect(config.airflowUrl).toBe("http://runtime-airflow.example.com");
  });

  it("falls back to a NEXT_PUBLIC env var when the window global omits the field", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    // Only apiBaseUrl is present in the window global; airflowUrl is absent.
    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://runtime.example.com");
    expect(config.airflowUrl).toBe("http://build-time-airflow.example.com");
  });

  it("falls back to the NEXT_PUBLIC env vars when the window global fields are empty strings", async () => {
    // Host `pnpm dev` against the cluster (README §Local development): the server
    // layout always injects the global, but with empty fields because the
    // server-side DATASPOKE_* vars are unset there — so resolution must treat ""
    // as "unset" (`||`, not `??`) for the .env.local values to reach the client.
    // This is the case that keeps `pnpm dev` able to talk to the in-cluster API.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "",
      airflowUrl: "",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://build-time.example.com");
    expect(config.airflowUrl).toBe("http://build-time-airflow.example.com");
  });
});
