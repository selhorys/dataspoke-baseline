"use client";

/**
 * EventsTable — renders the unified per-dataset event timeline as one table:
 * Time + status badge + optional `wrapper` tag + event_type + click-to-expand
 * detail. Pure presentational; the panel owns querying and pagination.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events panel).
 */

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Detail</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {events.length === 0 ? (
            <TableRow>
              <TableCell
                colSpan={4}
                className="text-center text-sm text-muted-foreground"
              >
                {emptyMessage}
              </TableCell>
            </TableRow>
          ) : (
            events.map((e) => (
              <TableRow key={e.id}>
                <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                  {formatDateTime(e.occurred_at, tz)}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant={eventStatusVariant(e.status)} className="text-xs">
                      {e.status}
                    </Badge>
                    {e.wrapper && (
                      <Badge variant="outline" className="text-xs">
                        wrapper
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-sm">{e.event_type}</TableCell>
                <TableCell className="max-w-[360px]">
                  <EventDetailCell detail={e.detail} />
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}
