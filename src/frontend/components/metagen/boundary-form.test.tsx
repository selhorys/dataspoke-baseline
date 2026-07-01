/**
 * Tests for BoundaryForm — the per-dataset metagen boundary editor.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §per-dataset page — the boundary write
 * fields are is_enabled and allowed[]. The save uses PUT (full replace), so both
 * fields must round-trip through the form.
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
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("BoundaryForm — allowed round-trip", () => {
  it("seeds allowed from initialValues and includes it in the PUT body", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({ allowed: ["dataset.description"] })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    // Add a second allowed aspect, then submit.
    fireEvent.click(screen.getByLabelText("column.description"));
    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const body = onSubmit.mock.calls[0][0];
    expect(body.allowed).toContain("dataset.description");
    expect(body.allowed).toContain("column.description");
  });

  it("preserves a previously-set allowed aspect when is_enabled is toggled", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({
            is_enabled: true,
            allowed: ["dataset.description"],
          })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    // Toggle is_enabled off without touching the allowed list.
    fireEvent.click(screen.getByLabelText("is_enabled"));
    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const body = onSubmit.mock.calls[0][0];
    expect(body.is_enabled).toBe(false);
    expect(body.allowed).toContain("dataset.description");
  });

  it("submits an empty allowed list when all aspects are cleared", async () => {
    const onSubmit = vi.fn<(body: MetagenBoundaryPutBody) => void>();
    render(
      <>
        <BoundaryForm
          formId="boundary-form"
          initialValues={makeBoundary({ allowed: ["dataset.description"] })}
          onSubmit={onSubmit}
        />
        <button type="submit" form="boundary-form">
          Save boundary
        </button>
      </>,
    );

    // Remove the seeded aspect.
    fireEvent.click(screen.getByLabelText("dataset.description"));
    fireEvent.click(screen.getByRole("button", { name: /save boundary/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].allowed).toEqual([]);
  });
});
