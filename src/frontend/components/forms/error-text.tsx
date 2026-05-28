import * as React from "react";

interface ErrorTextProps {
  message?: string;
  className?: string;
}

export function ErrorText({ message, className }: ErrorTextProps) {
  if (!message) return null;
  return (
    <p className={`text-sm text-destructive${className ? ` ${className}` : ""}`}>{message}</p>
  );
}
