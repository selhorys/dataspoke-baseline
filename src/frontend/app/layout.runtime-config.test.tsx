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
// the layout module imports. The font's only effect is a CSS variable class.
vi.mock("next/font/google", () => ({
  Inter: () => ({ variable: "--font-inter" }),
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
