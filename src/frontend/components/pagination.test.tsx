/**
 * Tests for the shared <Pagination> control + buildPageItems.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Pagination (shared component): page-size
 *     selector (20 / 50 / 100), Prev/Next, numbered pages with ellipsis, and an
 *     "M–N of T" label. Default page size 20.
 *   - Plan "Standard pagination control": size selector sets limit + resets
 *     offset to 0; numbered-page math; disabled at the ends.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

// Replace the Radix Select with a native-ish stub so options are statically in
// the DOM and clicks fire onValueChange without opening a jsdom portal. This is
// the same standard approach used by ingestion-source-list.test.tsx.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children?: React.ReactNode;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "select-root", "data-value": value },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectTrigger: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("div", { "data-testid": "select-trigger" }, children),
  SelectValue: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("span", null, children),
  SelectContent: ({
    children,
    onValueChange,
  }: {
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "select-content" },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectItem: ({
    value,
    children,
    onValueChange,
  }: {
    value: string;
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "button",
      {
        "data-testid": `page-size-${value}`,
        type: "button",
        onClick: () => onValueChange?.(value),
      },
      children,
    ),
}));

import { Pagination, buildPageItems, DEFAULT_PAGE_SIZE } from "./pagination";

// ---------------------------------------------------------------------------
// buildPageItems — numbered-page math with ellipsis
// ---------------------------------------------------------------------------
describe("buildPageItems", () => {
  it("lists every page (no ellipsis) when totalPages <= 7", () => {
    expect(buildPageItems(1, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(buildPageItems(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("always includes first and last page", () => {
    const items = buildPageItems(10, 20);
    expect(items[0]).toBe(1);
    expect(items[items.length - 1]).toBe(20);
  });

  it("shows the current page with one neighbour on each side", () => {
    const items = buildPageItems(10, 20);
    expect(items).toContain(9);
    expect(items).toContain(10);
    expect(items).toContain(11);
  });

  it("inserts a left ellipsis when the current page is far from the start", () => {
    const items = buildPageItems(10, 20);
    expect(items).toContain("ellipsis-left");
    expect(items).toContain("ellipsis-right");
  });

  it("omits the left ellipsis near the start", () => {
    const items = buildPageItems(2, 20);
    expect(items).not.toContain("ellipsis-left");
    expect(items).toContain("ellipsis-right");
  });

  it("omits the right ellipsis near the end", () => {
    const items = buildPageItems(19, 20);
    expect(items).toContain("ellipsis-left");
    expect(items).not.toContain("ellipsis-right");
  });
});

// ---------------------------------------------------------------------------
// Page-size selector — sets limit and resets offset to 0
// ---------------------------------------------------------------------------
describe("Pagination — page-size selector", () => {
  it("defaults the selector to DEFAULT_PAGE_SIZE (20)", () => {
    render(
      <Pagination offset={0} limit={DEFAULT_PAGE_SIZE} total={100} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(screen.getByTestId("select-root").getAttribute("data-value")).toBe("20");
  });

  it("offers 20 / 50 / 100 as the page-size options", () => {
    render(
      <Pagination offset={0} limit={20} total={100} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(screen.getByTestId("page-size-20")).toBeTruthy();
    expect(screen.getByTestId("page-size-50")).toBeTruthy();
    expect(screen.getByTestId("page-size-100")).toBeTruthy();
  });

  it("calls onLimit with the chosen size AND onOffset(0) when the size changes", () => {
    const onLimit = vi.fn();
    const onOffset = vi.fn();
    render(
      <Pagination offset={40} limit={20} total={100} onOffset={onOffset} onLimit={onLimit} />,
    );
    fireEvent.click(screen.getByTestId("page-size-50"));
    expect(onLimit).toHaveBeenCalledWith(50);
    // Changing page size must reset to the first page.
    expect(onOffset).toHaveBeenCalledWith(0);
  });
});

// ---------------------------------------------------------------------------
// Numbered pages + Prev/Next navigation
// ---------------------------------------------------------------------------
describe("Pagination — navigation", () => {
  it("renders the 'M–N of T' label", () => {
    render(
      <Pagination offset={0} limit={20} total={45} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(screen.getByText(/1.20 of 45/)).toBeTruthy();
  });

  it("marks the current page button with aria-current", () => {
    render(
      <Pagination offset={20} limit={20} total={100} onOffset={() => {}} onLimit={() => {}} />,
    );
    // offset 20 / limit 20 → page 2
    const page2 = screen.getByRole("button", { name: "2" });
    expect(page2.getAttribute("aria-current")).toBe("page");
  });

  it("jumps to the clicked page number via onOffset", () => {
    const onOffset = vi.fn();
    render(
      <Pagination offset={0} limit={20} total={100} onOffset={onOffset} onLimit={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "3" }));
    // page 3 → offset (3-1)*20 = 40
    expect(onOffset).toHaveBeenCalledWith(40);
  });

  it("steps Prev/Next by one page via onOffset", () => {
    const onOffset = vi.fn();
    render(
      <Pagination offset={20} limit={20} total={100} onOffset={onOffset} onLimit={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /previous/i }));
    expect(onOffset).toHaveBeenCalledWith(0);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(onOffset).toHaveBeenCalledWith(40);
  });
});

// ---------------------------------------------------------------------------
// Disabled at the ends
// ---------------------------------------------------------------------------
describe("Pagination — disabled at ends", () => {
  it("disables Previous on the first page", () => {
    render(
      <Pagination offset={0} limit={20} total={45} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(
      (screen.getByRole("button", { name: /previous/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("disables Next on the last page", () => {
    render(
      <Pagination offset={40} limit={20} total={45} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(
      (screen.getByRole("button", { name: /next/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("disables both when there is a single page", () => {
    render(
      <Pagination offset={0} limit={20} total={5} onOffset={() => {}} onLimit={() => {}} />,
    );
    expect(
      (screen.getByRole("button", { name: /previous/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: /next/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
