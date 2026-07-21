/**
 * Regression tests for the root layout's runtime-config injection.
 *
 * The layout injects window.__DATASPOKE_RUNTIME_CONFIG__ from server-only
 * DATASPOKE_* env vars read at request time. These tests guard three things:
 *   - The route segment stays dynamically rendered (`dynamic === "force-dynamic"`),
 *     so the env reads resolve per request rather than freezing at build time.
 *   - The injected <script> reflects the current DATASPOKE_API_BASE_URL and
 *     DATASPOKE_AIRFLOW_URL values, defaulting to empty strings when unset.
 *   - Operator-controlled values are escaped so they cannot terminate the inline
 *     <script> element and execute attacker markup.
 *
 * Spec trace:
 *   - spec/feature/FRONTEND_BASIC.md §Shell: "Airflow, ReDoc | Deployment-local |
 *     Runtime config only (`airflowUrl`, `apiBaseUrl`)" — those two are the whole
 *     of the injected config. The DataHub and Langfuse links resolve from
 *     `GET /spoke/common/peripheral-links` "only", so they are not injected here
 *     and are covered in lib/api/peripheral-links.test.tsx.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";

// next/font/google is a build-time loader unavailable under jsdom; stub it so
// the layout module imports. The layout wires the three-role font system
// (FRONTEND_BASIC.md §Design system › Typography): Inter (body), Space Grotesk
// (display), JetBrains Mono (mono). Each loader's only effect the code reads is
// the `variable` CSS-variable class, so each mock returns that field. The values
// are distinct test sentinels so an assertion on the rendered <body> className can
// prove every one of the three is wired (and fail if any single one is dropped).
const FONT_VAR = {
  inter: "sentinel-font-inter",
  spaceGrotesk: "sentinel-font-space-grotesk",
  jetbrainsMono: "sentinel-font-jetbrains-mono",
} as const;
vi.mock("next/font/google", () => ({
  Inter: () => ({ variable: "sentinel-font-inter" }),
  Space_Grotesk: () => ({ variable: "sentinel-font-space-grotesk" }),
  JetBrains_Mono: () => ({ variable: "sentinel-font-jetbrains-mono" }),
}));

// Keep the render lightweight and provider-agnostic; this suite only inspects
// the injected runtime-config <script> in <head>.
vi.mock("./providers", () => ({
  Providers: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import RootLayout, { dynamic } from "./layout";

const ENV_KEY = "DATASPOKE_API_BASE_URL";
const AIRFLOW_ENV_KEY = "DATASPOKE_AIRFLOW_URL";

function setEnv(key: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

// RootLayout returns <html><head><script .../></head>...; under jsdom React
// hoists the <head> contents onto document.head, so the injected runtime-config
// script is read from the document rather than the RTL container.
function renderLayoutScript(): string {
  render(
    <RootLayout>
      <div>child</div>
    </RootLayout>,
  );
  const script = Array.from(document.querySelectorAll("script")).find((s) =>
    s.innerHTML.includes("window.__DATASPOKE_RUNTIME_CONFIG__"),
  );
  return script?.innerHTML ?? "";
}

// RootLayout renders <body className={...}>. Under jsdom React may render the
// <body> nested in the RTL container or apply its attributes to document.body;
// locate whichever <body> carries the layout's font-sans class and return its
// className so the three-font wiring can be asserted regardless of placement.
function renderLayoutBodyClassName(): string {
  render(
    <RootLayout>
      <div>child</div>
    </RootLayout>,
  );
  const bodies = Array.from(document.querySelectorAll("body"));
  const body =
    bodies.find((b) => b.className.includes("font-sans")) ?? document.body;
  return body.className;
}

describe("RootLayout runtime-config injection", () => {
  const original = process.env[ENV_KEY];
  const originalAirflow = process.env[AIRFLOW_ENV_KEY];

  afterEach(() => {
    cleanup();
    // Renders append the injected script to document.head; clear them so each
    // case reads only its own injection.
    document
      .querySelectorAll("script")
      .forEach((s) => {
        if (s.innerHTML.includes("window.__DATASPOKE_RUNTIME_CONFIG__")) {
          s.remove();
        }
      });
    setEnv(ENV_KEY, original);
    setEnv(AIRFLOW_ENV_KEY, originalAirflow);
  });

  it("is configured for dynamic per-request rendering", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("wires all three font CSS-variable classes plus font-sans onto <body>", () => {
    // spec/feature/FRONTEND_BASIC.md §Design system › Typography: the three-role
    // font system (Inter body, Space Grotesk display, JetBrains Mono mono) is
    // wired via next/font in app/layout.tsx; <body> carries each loader's
    // `.variable` CSS-variable class plus the font-sans family. Asserting all
    // three sentinel variables means this fails if any single font is dropped
    // from the <body> className.
    const className = renderLayoutBodyClassName();

    expect(className).toContain(FONT_VAR.inter);
    expect(className).toContain(FONT_VAR.spaceGrotesk);
    expect(className).toContain(FONT_VAR.jetbrainsMono);
    expect(className).toContain("font-sans");
  });

  it("injects the request-time DATASPOKE_API_BASE_URL into the runtime config script", () => {
    process.env[ENV_KEY] = "http://api.test.example/";

    expect(renderLayoutScript()).toContain('"apiBaseUrl":"http://api.test.example/"');
  });

  it("injects an empty apiBaseUrl when DATASPOKE_API_BASE_URL is unset", () => {
    delete process.env[ENV_KEY];

    expect(renderLayoutScript()).toContain('"apiBaseUrl":""');
  });

  it("injects the request-time DATASPOKE_AIRFLOW_URL into the runtime config script", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell — Airflow is deployment-local and
    // resolves from "Runtime config only (`airflowUrl`, `apiBaseUrl`)".
    process.env[AIRFLOW_ENV_KEY] = "http://airflow.test.example";

    expect(renderLayoutScript()).toContain('"airflowUrl":"http://airflow.test.example"');
  });

  it("injects an empty airflowUrl when DATASPOKE_AIRFLOW_URL is unset", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell — "Each icon renders only when its
    // URL resolves non-empty", so an unset var must reach the client as "".
    delete process.env[AIRFLOW_ENV_KEY];

    expect(renderLayoutScript()).toContain('"airflowUrl":""');
  });

  it("injects exactly the two deployment-local keys", () => {
    // spec/feature/FRONTEND_BASIC.md §Shell resolution table — DataHub and
    // Langfuse resolve from `GET /spoke/common/peripheral-links` "only", so the
    // injected config carries no key for them; the client has no alternative
    // plane that could mask what the DB holds. Parsing the payload (rather than
    // asserting on substrings) is what makes an extra injected key fail here.
    process.env[ENV_KEY] = "http://api.test.example";
    process.env[AIRFLOW_ENV_KEY] = "http://airflow.test.example";

    const script = renderLayoutScript();
    const payload = script.slice(
      script.indexOf("{"),
      script.lastIndexOf("}") + 1,
    );
    const injected = JSON.parse(payload) as Record<string, string>;
    expect(Object.keys(injected).sort()).toEqual(["airflowUrl", "apiBaseUrl"]);
  });

  it("escapes '<' so an operator-set URL cannot terminate the inline script", () => {
    // The config is serialised into a dangerouslySetInnerHTML <script>. A value
    // containing "</script>" would close the element and let the remainder parse
    // as markup, so the emitted text must carry no literal "</script>" while the
    // payload still parses back to the original characters. That pair states the
    // invariant without pinning which escape sequence encodes it.
    process.env[ENV_KEY] = "http://api.test.example/</script><img src=x onerror=alert(1)>";

    const script = renderLayoutScript();

    expect(script).not.toContain("</script>");
    // Backstop: the escaping did not mangle the value — it round-trips intact.
    const payload = script.slice(script.indexOf("{"), script.lastIndexOf("}") + 1);
    const injected = JSON.parse(payload) as { apiBaseUrl: string };
    expect(injected.apiBaseUrl).toBe(
      "http://api.test.example/</script><img src=x onerror=alert(1)>",
    );
  });
});
