"use client";

/**
 * EventsSection — renders a list of MetaGen events with status badges.
 * Reuses eventStatusVariant and formatDateTime.
 */

import { Badge } from "@/components/ui/badge";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";
import type { MetagenEvent } from "@/types/metagen";

interface EventsSectionProps {
  events: MetagenEvent[];
  emptyMessage?: string;
}

export function EventsSection({
  events,
  emptyMessage = "No events yet.",
}: EventsSectionProps) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  return (
    <ul className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="flex flex-wrap items-start gap-3 text-sm">
          <span className="shrink-0 text-muted-foreground">
            {formatDateTime(e.occurred_at)}
          </span>
          <Badge variant={eventStatusVariant(e.status)} className="text-xs">
            {e.status}
          </Badge>
          <span>{e.event_type}</span>
          {e.detail && Object.keys(e.detail).length > 0 && (
            <span className="font-mono text-xs text-muted-foreground">
              {JSON.stringify(e.detail)}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
