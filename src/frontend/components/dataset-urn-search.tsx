"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface DatasetUrnSearchProps {
  /** The value currently applied to the backend request. */
  value: string;
  /** Called only after an explicit submit or clear. */
  onSubmit: (value: string) => void;
  className?: string;
}

/**
 * A server-backed dataset URN search. Typing is deliberately local: requests
 * change only on Search, Enter, or Clear so a paged result is never filtered
 * client-side while the user is composing a query.
 */
export function DatasetUrnSearch({ value, onSubmit, className }: DatasetUrnSearchProps) {
  const [draft, setDraft] = useState(value);

  useEffect(() => setDraft(value), [value]);

  function submit() {
    onSubmit(draft.trim());
  }

  return (
    <form
      className={className ?? "flex flex-wrap items-center gap-2"}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <Input
        className="h-8 w-64 text-xs"
        aria-label="Search dataset URN"
        placeholder="Filter by dataset URN…"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <Button type="submit" size="sm">Search</Button>
      {value && (
        <Button type="button" size="sm" variant="outline" onClick={() => {
          setDraft("");
          onSubmit("");
        }}>
          Clear
        </Button>
      )}
    </form>
  );
}
