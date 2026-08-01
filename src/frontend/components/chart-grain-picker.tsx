"use client";

/**
 * ChartGrainPicker — the display-grain control for a chart surface, sized and
 * styled to sit flush beside a <RangePicker> trigger in the same heading row.
 *
 * Grain governs only how already-fetched rows are collapsed before plotting
 * (see lib/chart-grain.ts); it adds no request parameter and must never enter a
 * query key. Owners persist the selection with usePersistedGrainState.
 */

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { CHART_GRAINS, type ChartGrain } from "@/lib/chart-grain";

const GRAIN_LABELS: Record<ChartGrain, string> = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
};

interface ChartGrainPickerProps {
  value: ChartGrain;
  onChange: (grain: ChartGrain) => void;
  className?: string;
}

export function ChartGrainPicker({
  value,
  onChange,
  className,
}: ChartGrainPickerProps) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as ChartGrain)}>
      <SelectTrigger
        aria-label="Chart grain"
        className={cn(
          // Matches the RangePicker trigger: outline button, sm height,
          // normal-weight label, muted chevron. The icon size mirrors what the
          // RangePicker's Button actually renders — its cva base pins every
          // descendant svg to size-4 at a specificity its icons' own h-3.5
          // cannot beat — so both triggers show a 16px chevron.
          "h-9 w-auto gap-2 px-3 font-normal hover:bg-accent hover:text-accent-foreground",
          "[&>svg]:size-4 [&>svg]:shrink-0",
          className,
        )}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        {CHART_GRAINS.map((grain) => (
          <SelectItem key={grain} value={grain}>
            {GRAIN_LABELS[grain]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
