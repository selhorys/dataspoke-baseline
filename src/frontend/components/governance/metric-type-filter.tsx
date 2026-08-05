"use client";

/**
 * MetricTypeFilter — a checkbox-group multi-select over the metric types
 * (ingestion-freshness / validation-score / doc-health) shown on the governance
 * dashboard. All boxes default to checked (the caller seeds `value` with every
 * type). An empty selection means genuinely empty: no metric survives it and the
 * page shows its filtered-empty state. There is no fallback to "all".
 *
 * Filtering happens client-side over the already-fetched enabled set; this
 * control adds no request parameter.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard (Metric view controls).
 */

import { Checkbox } from "@/components/ui/checkbox";
import { METRIC_TYPES, type MetricType } from "@/types/governance";

interface MetricTypeFilterProps {
  value: MetricType[];
  onChange: (next: MetricType[]) => void;
}

export function MetricTypeFilter({ value, onChange }: MetricTypeFilterProps) {
  function toggle(type: MetricType, checked: boolean) {
    if (checked) {
      if (value.includes(type)) return;
      // Preserve canonical order.
      onChange(METRIC_TYPES.filter((t) => t === type || value.includes(t)));
    } else {
      onChange(value.filter((t) => t !== type));
    }
  }

  return (
    <div
      className="flex flex-wrap items-center gap-4"
      role="group"
      aria-label="Filter metrics by type"
    >
      {METRIC_TYPES.map((type) => {
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
