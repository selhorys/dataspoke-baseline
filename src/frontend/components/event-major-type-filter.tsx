"use client";

/**
 * EventMajorTypeFilter — a checkbox-group multi-select over the unified
 * per-dataset event timeline's major types (INGESTION / VALIDATION / METAGEN).
 * All boxes default to checked (the caller seeds `value` with every type). An
 * empty selection means "none" — the page maps that to "all" when querying so
 * the table never goes blank, but the control itself reflects exactly what is
 * checked.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events filter).
 */

import { Checkbox } from "@/components/ui/checkbox";
import { EVENT_MAJOR_TYPES, type EventMajorType } from "@/types/data";

interface EventMajorTypeFilterProps {
  value: EventMajorType[];
  onChange: (next: EventMajorType[]) => void;
}

export function EventMajorTypeFilter({
  value,
  onChange,
}: EventMajorTypeFilterProps) {
  function toggle(type: EventMajorType, checked: boolean) {
    if (checked) {
      if (value.includes(type)) return;
      // Preserve canonical order.
      onChange(EVENT_MAJOR_TYPES.filter((t) => t === type || value.includes(t)));
    } else {
      onChange(value.filter((t) => t !== type));
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-4" role="group" aria-label="Filter events by type">
      {EVENT_MAJOR_TYPES.map((type) => {
        const checked = value.includes(type);
        return (
          <label
            key={type}
            className="flex cursor-pointer items-center gap-1.5 text-xs font-medium"
          >
            <Checkbox
              checked={checked}
              onCheckedChange={(c) => toggle(type, c === true)}
              aria-label={type}
            />
            {type}
          </label>
        );
      })}
    </div>
  );
}
