/**
 * Tests for SecretRefAuthoringGuide — the collapsible read-only authoring guide.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Components (SecretRefAuthoringGuide);
 * spec/feature/SECRET_RESOLUTION.md §Admin authoring guide.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { SecretRefAuthoringGuide } from "./secret-ref-authoring-guide";

describe("SecretRefAuthoringGuide", () => {
  it("offers the collapsible summary and the kubectl recipe with the security-prefix", () => {
    render(<SecretRefAuthoringGuide />);
    expect(
      screen.getByText(/how to author a new source-credential reference/i),
    ).toBeInTheDocument();
    // The kubectl create-secret recipe is rendered verbatim, carrying the
    // dataspoke-source-cred- name prefix (the security boundary).
    expect(
      screen.getByText(/kubectl create secret generic dataspoke-source-cred-/),
    ).toBeInTheDocument();
  });
});
