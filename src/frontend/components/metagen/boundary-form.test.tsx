/**
 * Tests for BoundaryForm — the per-dataset metagen boundary editor.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §per-dataset page — the boundary write
 * fields are is_enabled, allowed[], and owner. The save uses PUT (full replace),
 * so owner must round-trip through the form; a previously-set owner must survive
 * an edit to other fields rather than being silently cleared.
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { BoundaryForm } from "./boundary-form";
import type { MetagenBoundary, MetagenBoundaryPutBody } from "@/types/metagen";

// Radix Checkbox depends on ResizeObserver, which jsdom does not provide.
beforeAll(() => {
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

function makeBoundary(overrides: Partial<MetagenBoundary> = {}): MetagenBoundary {
  return {
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:hive,example_db.catalog.t,PROD)",
    is_enabled: true,
    allowed: ["dataset.description"],
    owner: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("BoundaryForm — owner round-trip", () => {
  it("seeds the owner input from initialValues and includes it in the PUT body", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({ owner: "data-stewards" })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    const ownerInput = screen.getByLabelText("owner") as HTMLInputElement;
    expect(ownerInput.value).toBe("data-stewards");

    fireEvent.change(ownerInput, { target: { value: "platform-team" } });
    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ owner: "platform-team" });
  });

  it("preserves a previously-set owner when only other fields are edited", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({ owner: "data-stewards", allowed: [] })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    // Toggle an allowed aspect without touching the owner field.
    fireEvent.click(screen.getByLabelText("column.description"));
    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const body = onSubmit.mock.calls[0][0];
    expect(body.owner).toBe("data-stewards");
    expect(body.allowed).toContain("column.description");
  });

  it("sends owner as null when the input is left empty", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({ owner: null })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    const ownerInput = screen.getByLabelText("owner") as HTMLInputElement;
    expect(ownerInput.value).toBe("");

    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].owner).toBeNull();
  });
});
