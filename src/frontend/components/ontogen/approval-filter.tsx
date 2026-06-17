"use client";

/**
 * ApprovalFilter — small Select control for the All / Approved / Unapproved
 * status filter carried by each result tab. Applied client-side via
 * filterByApproval.
 */

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ApprovalFilterMode } from "@/lib/ontogen-filter";

interface ApprovalFilterProps {
  value: ApprovalFilterMode;
  onChange: (mode: ApprovalFilterMode) => void;
}

export function ApprovalFilter({ value, onChange }: ApprovalFilterProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-muted-foreground">filter</span>
      <Select value={value} onValueChange={(v) => onChange(v as ApprovalFilterMode)}>
        <SelectTrigger className="h-8 w-36" aria-label="Status filter">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All</SelectItem>
          <SelectItem value="approved">Approved</SelectItem>
          <SelectItem value="unapproved">Unapproved</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
