/**
 * Tests for lib/runtime-config.ts — getRuntimeConfig fallback precedence.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "Airflow, ReDoc | Deployment-local |
 *     Runtime config only (`airflowUrl`, `apiBaseUrl`)". Those two deployment-local
 *     values are the whole of the runtime config; the externally-wired peripherals
 *     (DataHub, Langfuse) resolve from `GET /spoke/common/peripheral-links` and are
 *     therefore covered in lib/api/peripheral-links.test.tsx, not here.
 *   - spec/feature/FRONTEND_BASIC.md §Stack: "The API base URL is resolved at runtime
 *     (the server injects `DATASPOKE_API_BASE_URL` into the page; empty falls back to
 *     same-origin), not inlined at build time, so one image serves any environment."
 *     That is the only statement under spec/ about this resolution, and it covers just
 *     the top of the order (server injection) plus the terminal case (empty).
 *   - Resolution order, per field:
 *       window.__DATASPOKE_RUNTIME_CONFIG__  >  DATASPOKE_*  >  NEXT_PUBLIC_*  >  ""
 *
 *     Anchored in spec/feature/FRONTEND_BASIC.md §Stack, which carries the per-field
 *     priority table, the "an empty string counts as unset at every tier" rule, and the
 *     reason the `DATASPOKE_*` tier is deliberately not `NEXT_PUBLIC_*` (Next.js inlines
 *     only the latter, which is what preserves the one-image property).
 *     src/frontend/README.md §Production / runtime configuration restates the same order
 *     for implementers; the spec is the authority the assertions below derive from.
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
    // …and the server-side DATASPOKE_* vars too. Load-bearing: they sit ABOVE the
    // NEXT_PUBLIC_* pair in the resolution order, so an ambient value inherited from
    // the developer's shell (or from a .env file Vitest loaded) would make a
    // "nothing configured" case pass for the wrong reason and would silently outrank
    // the NEXT_PUBLIC_* value a fallback case is trying to observe.
    vi.stubEnv("DATASPOKE_API_BASE_URL", undefined as unknown as string);
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", undefined as unknown as string);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    // Restore any `window` stub (the SSR case removes the global entirely) BEFORE the
    // typeof check below, so the cleanup runs against the real jsdom window.
    vi.unstubAllGlobals();
    if (typeof window !== "undefined") {
      delete window.__DATASPOKE_RUNTIME_CONFIG__;
    }
  });

  it("returns empty strings when the window global, the DATASPOKE_* vars and the NEXT_PUBLIC_* vars are all unset", async () => {
    // -- Backstop: all three sources really are absent, so "" is the terminal tier of
    //    the order and not one source quietly supplying an empty value --
    expect(window.__DATASPOKE_RUNTIME_CONFIG__).toBeUndefined();
    expect(process.env.DATASPOKE_API_BASE_URL).toBeUndefined();
    expect(process.env.NEXT_PUBLIC_API_BASE_URL).toBeUndefined();

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

  // ── The server (SSR) branch — DATASPOKE_* ───────────────────────────────────
  //
  // Regression cover for issue #129. In a container the NEXT_PUBLIC_* vars are never
  // set, so a server branch that consults only them resolves to "" during SSR and the
  // first HTML response carries a relative Google sign-in href that 404s. Hydration
  // does not repair an already-rendered attribute, so the five cases below are the
  // unit-level statement of the full per-field resolution order:
  //   window.__DATASPOKE_RUNTIME_CONFIG__  >  DATASPOKE_*  >  NEXT_PUBLIC_*  >  ""
  //
  // Environment note: this file runs under jsdom (vitest.config.mts `environment: "jsdom"`),
  // so `window` exists by default in every other case here — the "SSR" case below removes
  // it explicitly with vi.stubGlobal so that it exercises the genuine server shape
  // (`typeof window === "undefined"`) rather than merely "the window global is absent".
  //
  // spec: src/frontend/README.md §Production / runtime configuration — "Server (SSR and
  //   Server Components, where that window global does not exist yet) — from `process.env`
  //   directly. Server-rendered markup therefore carries the deployed URLs, so absolute
  //   links such as the Google sign-in href are correct in the first HTML response rather
  //   than depending on hydration to repair them."
  // spec: spec/feature/FRONTEND_BASIC.md §Stack — "The API base URL is resolved at runtime
  //   (the server injects `DATASPOKE_API_BASE_URL` into the page; empty falls back to
  //   same-origin), not inlined at build time, so one image serves any environment."

  it("resolves the DATASPOKE_* env vars on the server, where `window` does not exist (the SSR branch)", async () => {
    // The deployed-container shape: the ConfigMap supplies DATASPOKE_*, nothing supplies
    // NEXT_PUBLIC_* (the beforeEach clears both, so they are provably unset here).
    vi.stubEnv("DATASPOKE_API_BASE_URL", "http://api.deployed.example.com");
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", "http://airflow.deployed.example.com");

    // Remove the jsdom `window` so this is the real Node/SSR shape rather than a browser
    // that merely lacks the injected global. Load-bearing: without it, an impl written as
    // `typeof window === "undefined" ? DATASPOKE_* : NEXT_PUBLIC_*` — the production-
    // equivalent restructure — would still pass here through the browser branch, and the
    // #129 server path would go untested. (`typeof` on a global whose value is `undefined`
    // yields "undefined", so the impl's guard takes the server branch.)
    vi.stubGlobal("window", undefined);

    // -- Backstop: this really is the server branch, and the lower tier is provably empty --
    expect(typeof window).toBe("undefined");
    expect(process.env.NEXT_PUBLIC_API_BASE_URL).toBeUndefined();
    expect(process.env.NEXT_PUBLIC_AIRFLOW_URL).toBeUndefined();

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // spec: README §Production — with NEXT_PUBLIC_* unset "the `DATASPOKE_*` values are
    //   what resolve". "" here is the #129 defect: it renders a relative /api/v1/... href.
    expect(config.apiBaseUrl).toBe("http://api.deployed.example.com");
    expect(config.airflowUrl).toBe("http://airflow.deployed.example.com");
  });

  it("prefers the DATASPOKE_* env vars over the NEXT_PUBLIC_* env vars", async () => {
    vi.stubEnv("DATASPOKE_API_BASE_URL", "http://api.deployed.example.com");
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", "http://airflow.deployed.example.com");
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    // Server shape: this contest only ever arises on the server, because Next.js inlines
    // only NEXT_PUBLIC_* into the browser bundle. Asserting it under jsdom's `window`
    // would pin an arrangement production never reaches.
    vi.stubGlobal("window", undefined);
    expect(typeof window).toBe("undefined");

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // spec: README §Production — the NEXT_PUBLIC_* pair is "a dev-only fallback
    //   (`.env.local`)", so a build-time value must never outrank the request-time one.
    expect(config.apiBaseUrl).toBe("http://api.deployed.example.com");
    expect(config.airflowUrl).toBe("http://airflow.deployed.example.com");
  });

  it("falls through to the NEXT_PUBLIC_* env vars when the DATASPOKE_* vars are empty strings", async () => {
    // Host `pnpm dev`: the DATASPOKE_* vars may be present-but-empty in the shell, while
    // .env.local carries the NEXT_PUBLIC_* values that point at the in-cluster API. An
    // empty string counts as unset (`||`, not `??`), so those .env.local values must win.
    vi.stubEnv("DATASPOKE_API_BASE_URL", "");
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", "");
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    // Server shape: `pnpm dev`'s Node process is where a present-but-empty DATASPOKE_*
    // is read at all.
    vi.stubGlobal("window", undefined);

    // -- Backstop: the empty values really are set, so "" is being treated as unset
    //    rather than simply being absent from the environment --
    expect(typeof window).toBe("undefined");
    expect(process.env.DATASPOKE_API_BASE_URL).toBe("");
    expect(process.env.DATASPOKE_AIRFLOW_URL).toBe("");

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // spec: README §Production — "Resolution is per field, and an empty string counts as
    //   unset: each of `apiBaseUrl` and `airflowUrl` takes the `DATASPOKE_*` value when it
    //   is non-empty, otherwise the matching `NEXT_PUBLIC_*` value".
    expect(config.apiBaseUrl).toBe("http://build-time.example.com");
    expect(config.airflowUrl).toBe("http://build-time-airflow.example.com");
  });

  it("resolves apiBaseUrl and airflowUrl at independent tiers when only one DATASPOKE_* var is set", async () => {
    // The mixed case: DATASPOKE_API_BASE_URL is configured but DATASPOKE_AIRFLOW_URL is
    // not, while both NEXT_PUBLIC_* values are present. Load-bearing for "per field":
    // every other case here sets both fields at the same tier, so an impl that made ONE
    // tier decision for the whole object (e.g. `Boolean(process.env.DATASPOKE_API_BASE_URL)`
    // selecting the source for both fields) would satisfy all of them. It cannot satisfy
    // this one — it would return the deployed apiBaseUrl together with an EMPTY airflowUrl,
    // silently hiding the Airflow icon on a deployment that only overrides the API URL.
    vi.stubEnv("DATASPOKE_API_BASE_URL", "http://api.deployed.example.com");
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", undefined as unknown as string);
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    vi.stubGlobal("window", undefined);

    // -- Backstop: the two DATASPOKE_* vars really are at different tiers --
    expect(typeof window).toBe("undefined");
    expect(process.env.DATASPOKE_API_BASE_URL).toBe("http://api.deployed.example.com");
    expect(process.env.DATASPOKE_AIRFLOW_URL).toBeUndefined();

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // spec: README §Production — "Resolution is per field … each of `apiBaseUrl` and
    //   `airflowUrl` takes the `DATASPOKE_*` value when it is non-empty, otherwise the
    //   matching `NEXT_PUBLIC_*` value" (see the ANCHOR CAVEAT in the file header).
    expect(config.apiBaseUrl).toBe("http://api.deployed.example.com");
    expect(config.airflowUrl).toBe("http://build-time-airflow.example.com");
  });

  it("prefers window.__DATASPOKE_RUNTIME_CONFIG__ over any process.env value", async () => {
    // All three sources populated with distinguishable values, so the assertion can only
    // pass via the top of the order rather than by two of them agreeing.
    //
    // Scope note: only the window-vs-NEXT_PUBLIC_* half of this contest is reachable in
    // production — a browser bundle never carries DATASPOKE_* (Next.js inlines only
    // NEXT_PUBLIC_*), and the server never has a `window`. The DATASPOKE_* value is set
    // here to pin the guard ORDER (window branch checked before any process.env read),
    // not to model a deployment state.
    vi.stubEnv("DATASPOKE_API_BASE_URL", "http://api.deployed.example.com");
    vi.stubEnv("DATASPOKE_AIRFLOW_URL", "http://airflow.deployed.example.com");
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://build-time.example.com");
    vi.stubEnv("NEXT_PUBLIC_AIRFLOW_URL", "http://build-time-airflow.example.com");

    window.__DATASPOKE_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://runtime.example.com",
      airflowUrl: "http://runtime-airflow.example.com",
    };

    const { getRuntimeConfig } = await import("./runtime-config");
    const config = getRuntimeConfig();

    // spec: README §Production — "Client — from `window.__DATASPOKE_RUNTIME_CONFIG__`,
    //   written by the inline script tag in `<head>`". The server branch's DATASPOKE_*
    //   read is a same-request mirror of that injection, not a competitor to it.
    expect(config.apiBaseUrl).toBe("http://runtime.example.com");
    expect(config.airflowUrl).toBe("http://runtime-airflow.example.com");
  });
});
