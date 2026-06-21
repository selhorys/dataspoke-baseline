/**
 * Tests for EventDetailCell — the shared event `detail` cell.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Events: the detail
 *     column truncates compact JSON (~30 chars) and is click-to-expand into a
 *     pretty-printed JSON dialog. Applies to both the Source Detail Events
 *     table and the per-dataset reverse-lookup event/ingestion table.
 *   - spec/feature/FRONTEND_METAGEN.md §Components MetagenEventTable: the
 *     conf-detail event detail cell truncates with click-to-expand pretty-JSON.
 */
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EventDetailCell } from "./event-detail-cell";

describe("EventDetailCell — empty detail", () => {
  it("renders an em-dash and no button when detail has no keys", () => {
    // `detail` is always present (backend default `{}`), so the empty case is
    // the no-keys object — not null/undefined.
    render(<EventDetailCell detail={{}} />);
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("EventDetailCell — populated detail", () => {
  const detail = {
    dry_run: false,
    discovered_urns_count: 42,
    emitted_urns_count: 42,
    platform: "postgres",
  };

  it("truncates the trigger text with an ellipsis and hides the full JSON inline", () => {
    render(<EventDetailCell detail={detail} />);
    const full = JSON.stringify(detail);
    expect(full.length).toBeGreaterThan(30);

    // The trigger has a STABLE accessible name independent of the data;
    // the truncated JSON is the visible (sighted-user) text content.
    const trigger = screen.getByRole("button", { name: "View event detail" });
    expect(trigger.textContent).toContain("…");
    expect((trigger.textContent ?? "").length).toBeLessThanOrEqual(31);
    expect(trigger.textContent).not.toBe(full);
    // Full compact JSON is not rendered inline before opening.
    expect(screen.queryByText(full)).toBeNull();
  });

  it("opens the dialog with pretty multi-line JSON on click", () => {
    render(<EventDetailCell detail={detail} />);
    fireEvent.click(screen.getByRole("button", { name: "View event detail" }));

    expect(screen.getByText("Event detail")).toBeTruthy();

    const pretty = JSON.stringify(detail, null, 2);
    // Pretty output is multi-line and indented.
    expect(pretty).toContain("\n");
    expect(pretty).toContain("  ");
    const pre = screen.getByText((_, node) => node?.tagName === "PRE");
    expect(pre.textContent).toBe(pretty);
    // All keys/values are present in the expanded view.
    expect(pre.textContent).toContain("discovered_urns_count");
    expect(pre.textContent).toContain("42");
    expect(pre.textContent).toContain("postgres");
  });

  it("renders the trigger as type=button so it never submits a form", () => {
    render(<EventDetailCell detail={detail} />);
    expect(
      screen
        .getByRole("button", { name: "View event detail" })
        .getAttribute("type"),
    ).toBe("button");
  });
});
