/**
 * Tests for lib/runtime-config.ts — getRuntimeConfig fallback precedence.
 *
 * Spec trace:
 *   - Task: runtime configuration — one image, configured by runtime env vars
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
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", undefined as unknown as string);
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_URL", undefined as unknown as string);
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
    expect(config.apiBaseUrl).toBe("");
    expect(config.datahubUrl).toBe("");
    expect(config.langfuseUrl).toBe("");
    expect(config.airflowUrl).toBe("");
  });

  it("falls back to NEXT_PUBLIC_API_BASE_URL when window global is absent", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.example.com");
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", "http://datahub.example.com");
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_URL", "http://langfuse.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://airflow.example.com");

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://api.example.com");
    expect(config.datahubUrl).toBe("http://datahub.example.com");
    expect(config.langfuseUrl).toBe("http://langfuse.example.com");
    expect(config.airflowUrl).toBe("http://airflow.example.com");
  });

  it("prefers window.__DATASPOKE_RUNTIME_CONFIG__ over NEXT_PUBLIC env vars", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", "http://build-time-hub.example.com");
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_URL", "http://build-time-langfuse.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
      datahubUrl: "http://runtime-hub.example.com",
      langfuseUrl: "http://runtime-langfuse.example.com",
      airflowUrl: "http://runtime-airflow.example.com",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://runtime.example.com");
    expect(config.datahubUrl).toBe("http://runtime-hub.example.com");
    expect(config.langfuseUrl).toBe("http://runtime-langfuse.example.com");
    expect(config.airflowUrl).toBe("http://runtime-airflow.example.com");
  });

  it("falls back to NEXT_PUBLIC env var when window global has an undefined field", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", "http://build-time-hub.example.com");

    // Only apiBaseUrl is set in the window global; datahubUrl is absent.
    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://runtime.example.com");
    expect(config.datahubUrl).toBe("http://build-time-hub.example.com");
  });

  it("falls back to NEXT_PUBLIC env var when window global field is an empty string", async () => {
    // Host `pnpm dev`: the server layout injects the global with empty fields
    // (DATASPOKE_API_BASE_URL unset), so the build-time NEXT_PUBLIC value must win.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_DATAHUB_URL", "http://build-time-hub.example.com");
    vi.stubEnv("NEXT_PUBLIC_LANGFUSE_URL", "http://build-time-langfuse.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "",
      datahubUrl: "",
      langfuseUrl: "",
      airflowUrl: "",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    expect(config.apiBaseUrl).toBe("http://build-time.example.com");
    expect(config.datahubUrl).toBe("http://build-time-hub.example.com");
    expect(config.langfuseUrl).toBe("http://build-time-langfuse.example.com");
    expect(config.airflowUrl).toBe("http://build-time-airflow.example.com");
  });
});
