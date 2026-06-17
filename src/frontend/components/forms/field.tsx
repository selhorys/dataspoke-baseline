import * as React from "react";
import { cn } from "@/lib/utils";
import { Label } from "@/components/ui/label";
import { ErrorText } from "./error-text";

interface FieldProps {
  label: string;
  htmlFor?: string;
  /** Static help text rendered persistently beneath the input; stays visible on error. */
  description?: React.ReactNode;
  error?: string;
  hint?: string;
  required?: boolean;
  className?: string;
  children: React.ReactNode;
}

export function Field({
  label,
  htmlFor,
  description,
  error,
  hint,
  required,
  className,
  children,
}: FieldProps) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label htmlFor={htmlFor}>
        {label}
        {required && <span className="ml-1 text-destructive">*</span>}
      </Label>
      {children}
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      <ErrorText message={error} />
    </div>
  );
}
