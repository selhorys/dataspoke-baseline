import * as React from "react";
import { cn } from "@/lib/utils";

interface FieldPanelProps {
  label: string;
  className?: string;
  children: React.ReactNode;
}

/**
 * FieldPanel — a bordered fieldset with the field name as its legend, matching
 * the dataset_filter panel (DatasetFilterView). Used for read-only view fields
 * that should read as boxed panels rather than bare label/value rows.
 */
export function FieldPanel({ label, className, children }: FieldPanelProps) {
  return (
    <fieldset className={cn("space-y-2 rounded-md border p-4", className)}>
      <legend className="px-1 text-sm font-medium text-muted-foreground">{label}</legend>
      <div className="text-sm">{children}</div>
    </fieldset>
  );
}
