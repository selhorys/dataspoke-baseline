import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Responsive layout wrapper for multi-field forms. Fields flow in two columns
 * from the `sm` breakpoint up (each roughly available-width ÷ columns) and
 * collapse to a single column on narrow viewports.
 *
 * A child field that must occupy the full row — textareas, Markdown/YAML/recipe/
 * code editors, full-width selects, file inputs, or error/helper/submit rows —
 * gets `className="sm:col-span-2"`.
 */
export type FormGridProps = React.HTMLAttributes<HTMLDivElement>;

export function FormGrid({ className, children, ...props }: FormGridProps) {
  return (
    <div className={cn("grid gap-x-6 gap-y-5 sm:grid-cols-2", className)} {...props}>
      {children}
    </div>
  );
}
