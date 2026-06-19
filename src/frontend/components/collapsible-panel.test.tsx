/**
 * Tests for the shared <CollapsiblePanel>.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page — titled foldable
 * section. Defaults open; the header toggles; an `actions` slot click does not
 * toggle the body.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { CollapsiblePanel } from "./collapsible-panel";

describe("CollapsiblePanel", () => {
  it("renders open by default and shows children", () => {
    render(
      <CollapsiblePanel title="Ingestion">
        <p>body content</p>
      </CollapsiblePanel>,
    );
    expect(screen.getByText("body content")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /ingestion/i }).getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("toggles the body when the header is clicked", () => {
    render(
      <CollapsiblePanel title="Validation">
        <p>body content</p>
      </CollapsiblePanel>,
    );
    const header = screen.getByRole("button", { name: /validation/i });
    fireEvent.click(header);
    expect(screen.queryByText("body content")).toBeNull();
    expect(header.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(header);
    expect(screen.getByText("body content")).toBeTruthy();
  });

  it("honors defaultOpen=false", () => {
    render(
      <CollapsiblePanel title="MetaGen" defaultOpen={false}>
        <p>body content</p>
      </CollapsiblePanel>,
    );
    expect(screen.queryByText("body content")).toBeNull();
  });

  it("an actions-slot click does not toggle the body", () => {
    const onAction = vi.fn();
    render(
      <CollapsiblePanel
        title="Events"
        actions={
          <button type="button" onClick={onAction}>
            range
          </button>
        }
      >
        <p>body content</p>
      </CollapsiblePanel>,
    );
    fireEvent.click(screen.getByRole("button", { name: /range/i }));
    expect(onAction).toHaveBeenCalledTimes(1);
    // Body still visible — the actions click did not fold the panel.
    expect(screen.getByText("body content")).toBeTruthy();
  });
});
