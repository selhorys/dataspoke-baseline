/**
 * Regression tests for the root layout's runtime-config injection.
 *
 * The layout injects window.__DATASPOKE_RUNTIME_CONFIG__ from server-only
 * DATASPOKE_* env vars read at request time. These tests guard two things:
 *   - The route segment stays dynamically rendered (`dynamic === "force-dynamic"`),
 *     so the env reads resolve per request rather than freezing at build time.
 *   - The injected <script> reflects the current DATASPOKE_API_BASE_URL value,
 *     defaulting to an empty string when the var is unset.
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
    if (original === undefined) {
      delete process.env[ENV_KEY];
    } else {
      process.env[ENV_KEY] = original;
    }
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
    const testUrl = "http://api.test.example/";
    process.env[ENV_KEY] = testUrl;

    expect(renderLayoutScript()).toContain(testUrl);
  });

  it("injects an empty apiBaseUrl when DATASPOKE_API_BASE_URL is unset", () => {
    delete process.env[ENV_KEY];

    expect(renderLayoutScript()).toContain('"apiBaseUrl":""');
  });

  it("injects the request-time DATASPOKE_LANGFUSE_PROJECT_ID into the runtime config script", () => {
    const originalProject = process.env.DATASPOKE_LANGFUSE_PROJECT_ID;
    process.env.DATASPOKE_LANGFUSE_PROJECT_ID = "dataspoke-project";
    try {
      expect(renderLayoutScript()).toContain('"langfuseProjectId":"dataspoke-project"');
    } finally {
      if (originalProject === undefined) {
        delete process.env.DATASPOKE_LANGFUSE_PROJECT_ID;
      } else {
        process.env.DATASPOKE_LANGFUSE_PROJECT_ID = originalProject;
      }
    }
  });

  it("injects an empty langfuseProjectId when DATASPOKE_LANGFUSE_PROJECT_ID is unset", () => {
    const originalProject = process.env.DATASPOKE_LANGFUSE_PROJECT_ID;
    delete process.env.DATASPOKE_LANGFUSE_PROJECT_ID;
    try {
      expect(renderLayoutScript()).toContain('"langfuseProjectId":""');
    } finally {
      if (originalProject !== undefined) {
        process.env.DATASPOKE_LANGFUSE_PROJECT_ID = originalProject;
      }
    }
  });
});
