import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { DatasetUrnSearch } from "./dataset-urn-search";

describe("DatasetUrnSearch", () => {
  it("does not apply typing until explicit Search", () => {
    const onSubmit = vi.fn();
    render(<DatasetUrnSearch value="" onSubmit={onSubmit} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Search dataset URN" }), {
      target: { value: "orders" },
    });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(onSubmit).toHaveBeenCalledWith("orders");
  });

  it("submits on Enter and clears the applied backend query", () => {
    const onSubmit = vi.fn();
    render(<DatasetUrnSearch value="sales" onSubmit={onSubmit} />);
    fireEvent.submit(screen.getByRole("textbox", { name: "Search dataset URN" }).closest("form")!);
    expect(onSubmit).toHaveBeenCalledWith("sales");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onSubmit).toHaveBeenLastCalledWith("");
  });
});
