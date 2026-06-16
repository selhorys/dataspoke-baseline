/**
 * Tests for TimeField — the compact, locale-independent 24-hour HH:mm input
 * used by RangePicker's datetime granularity (start/end time).
 *
 * Spec trace:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "in datetime the start/end time fields are 24-hour (HH:mm, no AM/PM)."
 *   - components/ui/time-field.tsx contract (read for exact behavior):
 *       • controlled value rendered verbatim;
 *       • a complete, valid "HH:mm" typed in onChange propagates EAGERLY
 *         (without a blur), but only when the raw input already equals its
 *         normalized form and differs from the current value;
 *       • partial/invalid intermediate input is held locally and NOT propagated
 *         until blur;
 *       • on blur, parseable-but-out-of-range input is CLAMPED (hours 0–23,
 *         minutes 0–59) and the normalized "HH:mm" is emitted;
 *       • on blur, unparseable input is DISCARDED — reverts to the last valid
 *         value, no onChange;
 *       • the field is a plain text input (no native time picker / AM-PM chrome).
 *
 * Selectors are semantic: the field exposes an aria-label, queried via
 * getByLabelText / getByRole("textbox").
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TimeField } from "./time-field";

describe("TimeField — rendering", () => {
  it("renders the controlled value", () => {
    render(<TimeField value="09:15" onChange={vi.fn()} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time") as HTMLInputElement;
    expect(input.value).toBe("09:15");
  });

  it("is a plain text input with no AM/PM affordance", () => {
    render(<TimeField value="13:00" onChange={vi.fn()} aria-label="Start time" />);
    // Exposed as a textbox (role) — not a native <input type="time"> stepper,
    // and there is no AM/PM text anywhere in the rendered output.
    const input = screen.getByRole("textbox", { name: "Start time" });
    expect(input).toHaveAttribute("type", "text");
    expect(screen.queryByText(/AM|PM/i)).not.toBeInTheDocument();
  });
});

describe("TimeField — valid input propagation", () => {
  it("propagates a complete valid HH:mm eagerly via onChange (no blur needed)", () => {
    const onChange = vi.fn();
    render(<TimeField value="09:15" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time");

    fireEvent.change(input, { target: { value: "14:30" } });

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("14:30");
  });

  it("accepts 24-hour values past noon (e.g. 23:45) — no AM/PM coercion", () => {
    const onChange = vi.fn();
    render(<TimeField value="00:00" onChange={onChange} aria-label="End time" />);
    const input = screen.getByLabelText("End time");

    fireEvent.change(input, { target: { value: "23:45" } });

    expect(onChange).toHaveBeenCalledWith("23:45");
  });

  it("does not re-emit when the typed value already equals the current value", () => {
    const onChange = vi.fn();
    render(<TimeField value="08:30" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time");

    fireEvent.change(input, { target: { value: "08:30" } });

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("TimeField — partial / invalid input held until blur", () => {
  it("does NOT propagate partial input while typing", () => {
    const onChange = vi.fn();
    render(<TimeField value="09:15" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time") as HTMLInputElement;

    // Intermediate keystrokes ("1", "14", "14:") are not complete valid HH:mm.
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.change(input, { target: { value: "14" } });
    fireEvent.change(input, { target: { value: "14:" } });

    expect(onChange).not.toHaveBeenCalled();
    // The draft is shown locally (not yet committed upstream).
    expect(input.value).toBe("14:");
  });

  it("clamps out-of-range parseable input on blur (25:99 → 23:59)", () => {
    const onChange = vi.fn();
    render(<TimeField value="09:15" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time") as HTMLInputElement;

    // "25:99" parses (HH:mm shape) but exceeds the 24-hour ranges; it is held
    // until blur (raw !== normalized → no eager emit), then clamped on blur.
    fireEvent.change(input, { target: { value: "25:99" } });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.blur(input);

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("23:59");
    expect(input.value).toBe("23:59");
  });

  it("discards unparseable input on blur — reverts to the last valid value", () => {
    const onChange = vi.fn();
    render(<TimeField value="09:15" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.blur(input);

    // Unparseable → no propagation, draft reverts to the controlled value.
    expect(onChange).not.toHaveBeenCalled();
    expect(input.value).toBe("09:15");
  });

  it("pads a single-digit-component time on blur (9:5 → 09:05)", () => {
    const onChange = vi.fn();
    render(<TimeField value="09:15" onChange={onChange} aria-label="Start time" />);
    const input = screen.getByLabelText("Start time") as HTMLInputElement;

    // "9:5" matches the \d{1,2}:\d{1,2} shape but isn't its own normalized form
    // (so it's held until blur), then normalizes to zero-padded "09:05".
    fireEvent.change(input, { target: { value: "9:5" } });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.blur(input);
    expect(onChange).toHaveBeenCalledWith("09:05");
    expect(input.value).toBe("09:05");
  });
});

describe("TimeField — external value sync", () => {
  it("resyncs the draft when the controlled value changes from outside", () => {
    const { rerender } = render(
      <TimeField value="09:15" onChange={vi.fn()} aria-label="Start time" />,
    );
    const input = screen.getByLabelText("Start time") as HTMLInputElement;
    expect(input.value).toBe("09:15");

    rerender(<TimeField value="22:00" onChange={vi.fn()} aria-label="Start time" />);
    expect(input.value).toBe("22:00");
  });
});
