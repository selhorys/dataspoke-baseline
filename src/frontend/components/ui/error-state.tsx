import * as React from "react";
import { cn } from "@/lib/utils";

interface ErrorStateProps extends React.HTMLAttributes<HTMLDivElement> {
  message?: string;
}

/**
 * Minimal error-state placeholder used across feature pages
 * when a query fails. Renders the error message in destructive text.
 */
export function ErrorState({
  message = "Something went wrong.",
  className,
  ...props
}: ErrorStateProps) {
  return (
    <div
      className={cn(
        "flex min-h-[80px] flex-col items-center justify-center py-4 text-sm text-destructive",
        className,
      )}
      {...props}
    >
      {message}
    </div>
  );
}
