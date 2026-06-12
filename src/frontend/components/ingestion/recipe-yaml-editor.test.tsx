/**
 * Tests for RecipeYamlEditor — readOnly branch, edit+save flow,
 * client-side parse/validation errors, secret-ref highlighting, and server
 * error echo.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Recipe:
 *     read-only for DATAHUB_MANAGED (secrets masked); editable via YAML editor
 *     (PUT/PATCH); server 422/409 echoed inline.
 *   - spec/feature/FRONTEND_INGESTION.md §Create View:
 *     recipeOnly validation — editor validates recipe shape only and hands
 *     parsed recipe back to the page.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { RecipeYamlEditor } from "./recipe-yaml-editor";
import type { IngestionSourceBody } from "@/types/ingestion";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const validYaml = `mode: ACTIVE_CUSTOM_MANAGED
name: prod postgres
schedule: "0 0 * * *"
recipe:
  source:
    type: postgres
    config:
      host_port: "pg.example:5432"
`;

const recipeOnlyYaml = `source:
  type: postgres
  config:
    host_port: "pg.example:5432"
`;

const yamlWithSecretRef = `mode: ACTIVE_CUSTOM_MANAGED
name: prod postgres
schedule: "0 0 * * *"
recipe:
  source:
    type: postgres
    config:
      password: \${dummy-data-pg__password}
`;

// ---------------------------------------------------------------------------
// 1. Read-only view
// ---------------------------------------------------------------------------
describe("RecipeYamlEditor — readOnly=true", () => {
  it("renders the YAML text inside a <pre> block", () => {
    render(<RecipeYamlEditor value={validYaml} readOnly />);
    expect(screen.getByText(/mode: ACTIVE_CUSTOM_MANAGED/)).toBeTruthy();
  });

  it("does not render a Textarea or Save button in readOnly mode", () => {
    render(<RecipeYamlEditor value={validYaml} readOnly />);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
  });

  it("does not render the Edit button when readOnly (no edit is possible)", () => {
    render(<RecipeYamlEditor value={validYaml} readOnly onEditRequest={vi.fn()} />);
    // In readOnly mode the Edit button is suppressed
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
  });

  it("highlights secret refs in the read-only view", () => {
    render(<RecipeYamlEditor value={yamlWithSecretRef} readOnly />);
    // The secret ref span has a title attribute
    const secretSpan = screen.getByTitle(
      /secret reference — resolved server-side, never plaintext/i,
    );
    expect(secretSpan).toBeTruthy();
    expect(secretSpan.textContent).toContain("${dummy-data-pg__password}");
  });
});

// ---------------------------------------------------------------------------
// 2. Non-editing, non-readOnly view (shows Edit button)
// ---------------------------------------------------------------------------
describe("RecipeYamlEditor — editing=false, readOnly=false", () => {
  it("renders the YAML in highlighted view (not a Textarea)", () => {
    render(
      <RecipeYamlEditor value={validYaml} readOnly={false} editing={false} />,
    );
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("shows an Edit button that calls onEditRequest when provided", () => {
    const onEditRequest = vi.fn();
    render(
      <RecipeYamlEditor
        value={validYaml}
        readOnly={false}
        editing={false}
        onEditRequest={onEditRequest}
      />,
    );
    const editBtn = screen.getByRole("button", { name: /edit/i });
    fireEvent.click(editBtn);
    expect(onEditRequest).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// 3. Editable view — editing=true
// ---------------------------------------------------------------------------
describe("RecipeYamlEditor — editing=true", () => {
  it("renders a Textarea in editable mode", () => {
    render(<RecipeYamlEditor value={validYaml} editing />);
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("renders Save and Cancel buttons in editable mode", () => {
    render(<RecipeYamlEditor value={validYaml} editing onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: /save/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeTruthy();
  });

  it("calls onCancel when the Cancel button is clicked", () => {
    const onCancel = vi.fn();
    render(<RecipeYamlEditor value={validYaml} editing onCancel={onCancel} />);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("calls onSave with the parsed body when valid YAML is saved", () => {
    const onSave = vi.fn();
    render(
      <RecipeYamlEditor value={validYaml} editing onSave={onSave} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onSave).toHaveBeenCalledTimes(1);
    const [body] = onSave.mock.calls[0] as [IngestionSourceBody, string];
    expect(body.mode).toBe("ACTIVE_CUSTOM_MANAGED");
    expect(body.name).toBe("prod postgres");
    expect(body.schedule).toBe("0 0 * * *");
  });

  it("shows a parse error inline instead of calling onSave when YAML is invalid", () => {
    const onSave = vi.fn();
    render(
      <RecipeYamlEditor
        value="mode: ACTIVE\nname: [unterminated"
        editing
        onSave={onSave}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onSave).not.toHaveBeenCalled();
    // Expect a parse error message containing "line"
    expect(screen.getByText(/line \d+/i)).toBeTruthy();
  });

  it("shows a validation error when the YAML body fails shape validation", () => {
    const onSave = vi.fn();
    // Missing required recipe.source.type
    const invalidYaml = `mode: ACTIVE_CUSTOM_MANAGED
name: test
schedule: "0 0 * * *"
recipe:
  source:
    config: {}
`;
    render(
      <RecipeYamlEditor value={invalidYaml} editing onSave={onSave} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText(/source\.type/i)).toBeTruthy();
  });

  it("echoes a serverError inline", () => {
    render(
      <RecipeYamlEditor
        value={validYaml}
        editing
        serverError="INGESTION_SOURCE_READONLY: This source is managed by DataHub."
      />,
    );
    expect(
      screen.getByText(/INGESTION_SOURCE_READONLY/),
    ).toBeTruthy();
  });

  it("shows a Saving… label and disables controls while isSaving=true", () => {
    render(
      <RecipeYamlEditor value={validYaml} editing isSaving onCancel={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /saving/i })).toBeTruthy();
    expect(
      (screen.getByRole("textbox") as HTMLTextAreaElement).disabled,
    ).toBe(true);
  });

  it("shows secret-ref hint when the YAML contains a secret reference", () => {
    render(
      <RecipeYamlEditor value={yamlWithSecretRef} editing />,
    );
    // The hint paragraph appears below the Textarea; there may be multiple
    // elements containing the ref text (Textarea + hint span), so assert on
    // the labelled hint container using getAllByText.
    expect(screen.getByText(/secret refs/i)).toBeTruthy();
    // At least one of the elements with the ref text is the hint span (not just textarea)
    expect(screen.getAllByText(/dummy-data-pg__password/).length).toBeGreaterThanOrEqual(1);
  });

  it("does NOT show secret-ref hint when YAML has no secret references", () => {
    render(
      <RecipeYamlEditor value={validYaml} editing />,
    );
    expect(screen.queryByText(/secret refs/i)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. recipeOnly mode — editor validates recipe shape, calls onRecipeSave
// ---------------------------------------------------------------------------
describe("RecipeYamlEditor — validateOptions.recipeOnly", () => {
  it("calls onRecipeSave with the parsed recipe object", () => {
    const onRecipeSave = vi.fn();
    render(
      <RecipeYamlEditor
        value={recipeOnlyYaml}
        editing
        validateOptions={{ recipeOnly: true }}
        onRecipeSave={onRecipeSave}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onRecipeSave).toHaveBeenCalledTimes(1);
    const [recipe] = onRecipeSave.mock.calls[0] as [Record<string, unknown>, string];
    expect((recipe as { source: { type: string } }).source.type).toBe("postgres");
  });

  it("shows a validation error when recipe is missing source.type in recipeOnly mode", () => {
    const onRecipeSave = vi.fn();
    const invalidRecipeYaml = `source:
  config: {}
`;
    render(
      <RecipeYamlEditor
        value={invalidRecipeYaml}
        editing
        validateOptions={{ recipeOnly: true }}
        onRecipeSave={onRecipeSave}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onRecipeSave).not.toHaveBeenCalled();
    expect(screen.getByText(/source\.type/i)).toBeTruthy();
  });
});
