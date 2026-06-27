import * as React from "react";
import { cn } from "@/lib/utils";
import { Label } from "@/components/ui/label";

interface FieldValueProps {
  label: string;
  className?: string;
  children: React.ReactNode;
}

/**
 * FieldValue — the read-only analogue of Field: a label above a plain-text value,
 * matching Field's label spacing so view and edit surfaces line up.
 */
export function FieldValue({ label, className, children }: FieldValueProps) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label>{label}</Label>
      <div className="text-sm">{children}</div>
    </div>
  );
}
