"use client";

/**
 * EventDetailCell — renders an event's `detail` JSON in a table cell. Empty
 * detail shows a muted em-dash; otherwise the compact JSON is truncated to MAX
 * chars and is a click-to-expand trigger that opens a modal with the
 * pretty-printed JSON.
 *
 * Shared across the ingestion event tables and the metagen conf-detail event
 * table (spec/feature/FRONTEND_INGESTION.md §Events,
 * spec/feature/FRONTEND_METAGEN.md §Components MetagenEventTable).
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const MAX = 30;

interface EventDetailCellProps {
  detail: Record<string, unknown>;
}

export function EventDetailCell({ detail }: EventDetailCellProps) {
  const [open, setOpen] = useState(false);

  if (Object.keys(detail).length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  const compact = JSON.stringify(detail);
  const label = compact.length > MAX ? `${compact.slice(0, MAX)}…` : compact;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label="View event detail"
        className="h-6 px-1.5 font-mono text-xs text-muted-foreground"
        onClick={() => setOpen(true)}
      >
        {label}
      </Button>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Event detail</DialogTitle>
        </DialogHeader>
        <pre className="max-h-[60vh] overflow-auto rounded bg-muted p-3 text-xs">
          {JSON.stringify(detail, null, 2)}
        </pre>
      </DialogContent>
    </Dialog>
  );
}
