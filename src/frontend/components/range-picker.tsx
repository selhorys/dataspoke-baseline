"use client";

/**
 * RangePicker — standardized date/time range control.
 *
 * Trigger shows the current range (in the active timezone); a three-column
 * popover offers preset shortcuts plus two independent single-day calendars
 * (start | end, with month arrows and a month/year dropdown each), and
 * start/end time fields in datetime granularity. Every edit — including clicking
 * a preset — is staged locally and committed to onChange only on Apply; Cancel
 * discards.
 *
 * Emits canonical inclusive {from, to} ISO-8601 (UTC) strings; see lib/range.ts.
 * The `tz` prop (the global display timezone) only governs interpretation/
 * display — the emitted bounds are always absolute UTC instants.
 */

import * as React from "react";
import { CalendarIcon, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TimeField } from "@/components/ui/time-field";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import {
  RANGE_PRESETS,
  resolveRange,
  selectionLabel,
  type RangeGranularity,
  type RangeSelection,
  type TzMode,
} from "@/lib/range";

interface RangePickerProps {
  value: RangeSelection;
  onChange: (value: RangeSelection) => void;
  tz: TzMode;
  granularity?: RangeGranularity;
  className?: string;
}

// ── ISO <-> calendar helpers (tz-aware) ────────────────────────────────────────

/** Convert an ISO instant to a noon-anchored calendar Date for the grid, using
 *  the wall-clock day in `tz`. Anchoring at noon avoids the off-by-one a raw
 *  `new Date(iso)` can cause across DST/offset boundaries. */
function isoToCalendarDate(iso: string, tz: TzMode): Date {
  const d = new Date(iso);
  if (tz === "utc") {
    return new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 12);
  }
  return new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12);
}

/** "HH:mm" for the time fields, read as the wall-clock time in `tz`. */
function isoToTime(iso: string, tz: TzMode): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  if (tz === "utc") {
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Compose a calendar Date + "HH:mm" into a UTC ISO string, interpreting the
 *  day/time as a wall-clock value in `tz`. */
function composeIso(
  day: Date,
  time: string | null,
  granularity: RangeGranularity,
  bound: "from" | "to",
  tz: TzMode,
): string {
  let hours: number;
  let minutes: number;
  let seconds: number;
  let ms: number;
  if (granularity === "datetime" && time) {
    const [h, m] = time.split(":").map((p) => Number.parseInt(p, 10));
    hours = Number.isNaN(h) ? 0 : h;
    minutes = Number.isNaN(m) ? 0 : m;
    seconds = bound === "to" ? 59 : 0;
    ms = bound === "to" ? 999 : 0;
  } else {
    // Date granularity bounds whole days (in tz).
    hours = bound === "to" ? 23 : 0;
    minutes = bound === "to" ? 59 : 0;
    seconds = bound === "to" ? 59 : 0;
    ms = bound === "to" ? 999 : 0;
  }
  const instant =
    tz === "utc"
      ? new Date(
          Date.UTC(day.getFullYear(), day.getMonth(), day.getDate(), hours, minutes, seconds, ms),
        )
      : new Date(day.getFullYear(), day.getMonth(), day.getDate(), hours, minutes, seconds, ms);
  return instant.toISOString();
}

export function RangePicker({
  value,
  onChange,
  tz,
  granularity = "date",
  className,
}: RangePickerProps) {
  const [open, setOpen] = React.useState(false);

  // Staged draft, seeded from `value` whenever the popover opens so Cancel can
  // discard cleanly. `draftDays` is non-null while a preset is staged and drops
  // to null as soon as the user touches a calendar day or a time field (→
  // custom). `leftMonth` / `rightMonth` are the controlled displayed months of
  // the two calendars.
  const [draftFrom, setDraftFrom] = React.useState<Date | undefined>();
  const [draftTo, setDraftTo] = React.useState<Date | undefined>();
  const [draftDays, setDraftDays] = React.useState<number | null>(null);
  const [fromTime, setFromTime] = React.useState("00:00");
  const [toTime, setToTime] = React.useState("23:59");
  const [leftMonth, setLeftMonth] = React.useState<Date>(() => new Date());
  const [rightMonth, setRightMonth] = React.useState<Date>(() => new Date());

  // First-of-month for the given calendar Date (controlled-month anchor).
  const firstOfMonth = (d: Date) => new Date(d.getFullYear(), d.getMonth(), 1);

  // Resolve the selection to concrete bounds for seeding the calendars/time
  // fields — so opening while a preset is active starts the edit from that
  // preset's concrete window (and keeps the preset staged for highlight).
  const seedDraft = React.useCallback(() => {
    const resolved = resolveRange(value, granularity, tz);
    const from = isoToCalendarDate(resolved.from, tz);
    const to = isoToCalendarDate(resolved.to, tz);
    setDraftFrom(from);
    setDraftTo(to);
    setDraftDays(value.kind === "preset" ? value.days : null);
    setFromTime(isoToTime(resolved.from, tz));
    setToTime(isoToTime(resolved.to, tz));
    setLeftMonth(firstOfMonth(from));
    setRightMonth(firstOfMonth(to));
  }, [value, granularity, tz]);

  const handleOpenChange = (next: boolean) => {
    if (next) seedDraft();
    setOpen(next);
  };

  // Stage a preset — moves both calendars to the preset window. Does NOT close
  // or commit; only Apply commits.
  const handlePreset = (days: number) => {
    const resolved = resolveRange({ kind: "preset", days }, granularity, tz);
    const from = isoToCalendarDate(resolved.from, tz);
    const to = isoToCalendarDate(resolved.to, tz);
    setDraftFrom(from);
    setDraftTo(to);
    setDraftDays(days);
    setFromTime(isoToTime(resolved.from, tz));
    setToTime(isoToTime(resolved.to, tz));
    setLeftMonth(firstOfMonth(from));
    setRightMonth(firstOfMonth(to));
  };

  const inRange = React.useCallback(
    (day: Date) => !!draftFrom && !!draftTo && day > draftFrom && day < draftTo,
    [draftFrom, draftTo],
  );

  const handleApply = () => {
    if (!draftFrom) {
      setOpen(false);
      return;
    }
    if (draftDays != null) {
      onChange({ kind: "preset", days: draftDays });
    } else {
      onChange({
        kind: "custom",
        from: composeIso(draftFrom, fromTime, granularity, "from", tz),
        to: composeIso(draftTo ?? draftFrom, toTime, granularity, "to", tz),
      });
    }
    setOpen(false);
  };

  // Bounds for the month/year dropdown. `new Date()` here only sizes the year
  // range — it is not a query key, so re-evaluating per render is fine.
  const startMonth = new Date(2015, 0);
  const endMonth = new Date(new Date().getFullYear() + 1, 11);
  const bandModifiers = { inRange };
  const bandClassNames = { inRange: "bg-primary/15 rounded-none" };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn("justify-start gap-2 font-normal", className)}
        >
          <CalendarIcon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{selectionLabel(value, granularity, tz)}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-0" align="end">
        <div className="flex flex-col">
          {/* presets | LEFT (start) calendar | RIGHT (end) calendar */}
          <div className="flex flex-col sm:flex-row">
            {/* Presets — staged on click, never committed here. */}
            <div className="flex flex-col gap-1 border-b p-2 sm:border-b-0 sm:border-r">
              {RANGE_PRESETS.map((preset) => (
                <Button
                  key={preset.days}
                  variant={draftDays === preset.days ? "secondary" : "ghost"}
                  size="sm"
                  className="justify-start font-normal"
                  onClick={() => handlePreset(preset.days)}
                >
                  {preset.label}
                </Button>
              ))}
            </div>

            {/* LEFT calendar — start day. */}
            <div className="flex flex-col gap-2 p-2">
              <Calendar
                mode="single"
                selected={draftFrom}
                month={leftMonth}
                onMonthChange={setLeftMonth}
                onSelect={(d) => {
                  if (!d) return;
                  setDraftFrom(d);
                  setDraftTo((t) => (t && d > t ? d : t));
                  setDraftDays(null);
                }}
                modifiers={bandModifiers}
                modifiersClassNames={bandClassNames}
                captionLayout="dropdown"
                startMonth={startMonth}
                endMonth={endMonth}
                fixedWeeks
              />
              {granularity === "datetime" && (
                <div className="mt-auto flex flex-col gap-1 px-1 pt-1 text-xs text-muted-foreground">
                  <label htmlFor="range-start-time">Start time</label>
                  <TimeField
                    id="range-start-time"
                    aria-label="Start time"
                    value={fromTime}
                    onChange={(v) => {
                      setFromTime(v);
                      setDraftDays(null);
                    }}
                  />
                </div>
              )}
            </div>

            {/* RIGHT calendar — end day. */}
            <div className="flex flex-col gap-2 border-t p-2 sm:border-l sm:border-t-0">
              <Calendar
                mode="single"
                selected={draftTo}
                month={rightMonth}
                onMonthChange={setRightMonth}
                onSelect={(d) => {
                  if (!d) return;
                  setDraftTo(d);
                  setDraftFrom((f) => (f && d < f ? d : f));
                  setDraftDays(null);
                }}
                modifiers={bandModifiers}
                modifiersClassNames={bandClassNames}
                captionLayout="dropdown"
                startMonth={startMonth}
                endMonth={endMonth}
                fixedWeeks
              />
              {granularity === "datetime" && (
                <div className="mt-auto flex flex-col gap-1 px-1 pt-1 text-xs text-muted-foreground">
                  <label htmlFor="range-end-time">End time</label>
                  <TimeField
                    id="range-end-time"
                    aria-label="End time"
                    value={toTime}
                    onChange={(v) => {
                      setToTime(v);
                      setDraftDays(null);
                    }}
                  />
                </div>
              )}
            </div>
          </div>

          {/* Shared footer — Cancel/Apply. */}
          <div className="flex items-center justify-end gap-2 border-t p-2">
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" onClick={handleApply} disabled={!draftFrom}>
              Apply
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
