"use client";

/**
 * SortControl — small Select for ordering an ontogen result table by
 * creation time. Maps to the server-side `?sort=` param (created_at_desc /
 * created_at_asc); the default is newest-first (created_at_desc).
 */

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export type OntogenSortMode = "created_at_desc" | "created_at_asc";

interface SortControlProps {
  value: OntogenSortMode;
  onChange: (mode: OntogenSortMode) => void;
}

export function SortControl({ value, onChange }: SortControlProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-muted-foreground">sort</span>
      <Select value={value} onValueChange={(v) => onChange(v as OntogenSortMode)}>
        <SelectTrigger className="h-8 w-44" aria-label="Sort order">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="created_at_desc">Created (newest)</SelectItem>
          <SelectItem value="created_at_asc">Created (oldest)</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
