"use client";

/**
 * EventsTable — renders the unified per-dataset event timeline rows: occurred_at
 * + status badge + optional `wrapper` tag + event_type + click-to-expand detail.
 * Pure presentational; the panel owns querying and pagination.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events panel).
 */

import { Badge } from "@/components/ui/badge";
import { EventDetailCell } from "@/components/ingestion/event-detail-cell";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { DatasetEvent } from "@/types/data";

interface EventsTableProps {
  events: DatasetEvent[];
  emptyMessage?: string;
}

export function EventsTable({
  events,
  emptyMessage = "No events yet.",
}: EventsTableProps) {
  const tz = useDisplayTz();

  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  return (
    <ul className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="flex flex-wrap items-start gap-3 text-sm">
          <span className="shrink-0 text-muted-foreground">
            {formatDateTime(e.occurred_at, tz)}
          </span>
          <Badge variant={eventStatusVariant(e.status)} className="text-xs">
            {e.status}
          </Badge>
          {e.wrapper && (
            <Badge variant="outline" className="text-xs">
              wrapper
            </Badge>
          )}
          <span>{e.event_type}</span>
          <EventDetailCell detail={e.detail} />
        </li>
      ))}
    </ul>
  );
}
