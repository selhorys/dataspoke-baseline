"use client";

/**
 * DEV FIXTURE — RangePicker sandbox. Not part of the product; safe to delete.
 * Renders the date and datetime RangePicker variants side by side and shows the
 * committed selection, the active (global) timezone, plus the resolved {from,to}
 * bounds so the popover, presets, persistence, and both granularities can be
 * exercised on localhost. Display timezone follows the global preference
 * (Settings → Timezone).
 *
 * Visit: http://localhost:3000/dev/range-picker
 */

import * as React from "react";
import { RangePicker } from "@/components/range-picker";
import {
  defaultSelection,
  resolveRange,
  type RangeSelection,
} from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { useDisplayTz } from "@/lib/preferences/timezone";

function Panel({
  title,
  granularity,
  selection,
  onChange,
}: {
  title: string;
  granularity: "date" | "datetime";
  selection: RangeSelection;
  onChange: (s: RangeSelection) => void;
}) {
  const tz = useDisplayTz();
  const resolved = resolveRange(selection, granularity, tz);
  return (
    <section className="space-y-3 rounded-lg border p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium">
          {title}{" "}
          <span className="text-xs text-muted-foreground">
            (granularity=&quot;{granularity}&quot;)
          </span>
        </h2>
        <RangePicker
          value={selection}
          onChange={onChange}
          tz={tz}
          granularity={granularity}
        />
      </div>
      <pre className="overflow-x-auto rounded-md bg-muted/50 p-3 text-xs">
        {JSON.stringify({ selection, tz, resolved }, null, 2)}
      </pre>
    </section>
  );
}

export default function RangePickerSandboxPage() {
  // Ephemeral selections (not persisted) — exercise the raw component.
  const [dateSel, setDateSel] = React.useState<RangeSelection>(() =>
    defaultSelection(),
  );
  const [dtSel, setDtSel] = React.useState<RangeSelection>(() =>
    defaultSelection(),
  );

  // Persisted state — exercise localStorage round-trip across reloads.
  const { selection, setSelection } = usePersistedRangeState(
    RANGE_KEYS.ingestionSourceEvents,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">
          RangePicker sandbox
        </h1>
        <p className="text-sm text-muted-foreground">
          Dev fixture — exercise the date and datetime variants. Display timezone
          follows the global preference (Settings → Timezone). The third panel is
          persisted to localStorage (reload to confirm the selection sticks).
        </p>
      </div>

      <Panel
        title="Date variant"
        granularity="date"
        selection={dateSel}
        onChange={setDateSel}
      />
      <Panel
        title="Datetime variant"
        granularity="datetime"
        selection={dtSel}
        onChange={setDtSel}
      />
      <Panel
        title="Datetime + persisted (reload to verify)"
        granularity="datetime"
        selection={selection}
        onChange={setSelection}
      />
    </div>
  );
}
