/**
 * Tests for <EventMajorTypeFilter>.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events filter) —
 * checkbox-group multi-select over INGESTION / VALIDATION / METAGEN. Toggling a
 * box adds/removes its type, preserving canonical order.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { EventMajorTypeFilter } from "./event-major-type-filter";
import { EVENT_MAJOR_TYPES, type EventMajorType } from "@/types/data";

describe("EventMajorTypeFilter", () => {
  it("renders a checkbox per major type, reflecting the value", () => {
    render(
      <EventMajorTypeFilter value={[...EVENT_MAJOR_TYPES]} onChange={vi.fn()} />,
    );
    for (const t of EVENT_MAJOR_TYPES) {
      const box = screen.getByRole("checkbox", { name: t });
      expect(box.getAttribute("aria-checked")).toBe("true");
    }
  });

  it("unchecking a type removes it from the selection", () => {
    const onChange = vi.fn();
    render(
      <EventMajorTypeFilter value={[...EVENT_MAJOR_TYPES]} onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "VALIDATION" }));
    expect(onChange).toHaveBeenCalledWith(["INGESTION", "METAGEN"]);
  });

  it("checking a type adds it back in canonical order", () => {
    const onChange = vi.fn();
    const value: EventMajorType[] = ["INGESTION", "METAGEN"];
    render(<EventMajorTypeFilter value={value} onChange={onChange} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "VALIDATION" }));
    expect(onChange).toHaveBeenCalledWith([
      "INGESTION",
      "VALIDATION",
      "METAGEN",
    ]);
  });
});
