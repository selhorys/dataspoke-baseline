import * as React from "react";
import { cn } from "@/lib/utils";

interface EmptyStateProps extends React.HTMLAttributes<HTMLDivElement> {
  message?: string;
}

/**
 * Minimal empty-state placeholder used across feature list views
 * when a query returns zero items.
 */
export function EmptyState({ message = "No data found.", className, ...props }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex min-h-[120px] flex-col items-center justify-center py-8 text-sm text-muted-foreground",
        className,
      )}
      {...props}
    >
      {message}
    </div>
  );
}
