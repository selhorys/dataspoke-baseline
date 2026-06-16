"use client";

/**
 * TimeField — a compact, locale-independent 24-hour HH:mm input.
 *
 * Replaces the native <input type="time"> (which renders AM/PM and a stepper in
 * many locales and varies in width). Accepts free typing of "HH:mm"; on blur it
 * clamps/normalizes to a valid 24-hour value (hours 00–23, minutes 00–59) and
 * emits the normalized "HH:mm". Invalid intermediate input is held locally and
 * not propagated until blur.
 */

import * as React from "react";
import { cn } from "@/lib/utils";

interface TimeFieldProps {
  /** Controlled value in "HH:mm" (24-hour). */
  value: string;
  /** Called with a normalized "HH:mm" string. */
  onChange: (value: string) => void;
  "aria-label"?: string;
  id?: string;
  className?: string;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Parse + clamp arbitrary input to a valid "HH:mm", or null if unparseable. */
function normalize(raw: string): string | null {
  const trimmed = raw.trim();
  const m = /^(\d{1,2})\s*:\s*(\d{1,2})$/.exec(trimmed);
  if (!m) return null;
  let h = Number.parseInt(m[1], 10);
  let mi = Number.parseInt(m[2], 10);
  if (Number.isNaN(h) || Number.isNaN(mi)) return null;
  h = Math.min(23, Math.max(0, h));
  mi = Math.min(59, Math.max(0, mi));
  return `${pad(h)}:${pad(mi)}`;
}

export function TimeField({
  value,
  onChange,
  "aria-label": ariaLabel,
  id,
  className,
}: TimeFieldProps) {
  // Local draft so intermediate (invalid) keystrokes don't propagate; resync
  // when the controlled value changes from outside.
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => {
    setDraft(value);
  }, [value]);

  const commit = () => {
    const normalized = normalize(draft);
    if (normalized) {
      setDraft(normalized);
      if (normalized !== value) onChange(normalized);
    } else {
      // Discard unparseable input — revert to the last valid value.
      setDraft(value);
    }
  };

  const handleChange = (raw: string) => {
    setDraft(raw);
    // Propagate eagerly when the input is already a complete, valid HH:mm so the
    // edit takes effect without requiring a blur; partial/invalid input waits
    // for blur to normalize.
    const normalized = normalize(raw);
    if (normalized && normalized === raw && normalized !== value) {
      onChange(normalized);
    }
  };

  return (
    <input
      id={id}
      type="text"
      inputMode="numeric"
      maxLength={5}
      placeholder="HH:mm"
      aria-label={ariaLabel}
      value={draft}
      onChange={(e) => handleChange(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.currentTarget.blur();
        }
      }}
      className={cn(
        "h-8 w-[72px] rounded-md border border-input bg-background px-2 text-sm tabular-nums ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    />
  );
}
TimeField.displayName = "TimeField";
