"use client";

/**
 * IngestionEventTable — run/event history for a source, newest first.
 *
 * Columns: occurred_at, status badge (via eventStatusVariant), event_type,
 * detail (truncated JSON, click-to-expand via EventDetailCell). Includes a
 * datetime range filter and pagination.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Events.
 */

import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { EventDetailCell } from "@/components/ui/event-detail-cell";
import { Pagination } from "@/components/pagination";
import { RangePicker } from "@/components/range-picker";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";
import type { RangeSelection, TzMode } from "@/lib/range";
import type { IngestionEvent } from "@/types/ingestion";

interface IngestionEventTableProps {
  events: IngestionEvent[];
  range: RangeSelection;
  onRangeChange: (value: RangeSelection) => void;
  tz: TzMode;
  page: { offset: number; limit: number; totalCount: number };
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

export function IngestionEventTable({
  events,
  range,
  onRangeChange,
  tz,
  page,
  onOffset,
  onLimit,
}: IngestionEventTableProps) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <RangePicker
          value={range}
          onChange={onRangeChange}
          tz={tz}
          granularity="datetime"
        />
      </div>

      {events.length === 0 ? (
        <EmptyState message="No events in this range." />
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>occurred_at</TableHead>
                <TableHead>status</TableHead>
                <TableHead>event_type</TableHead>
                <TableHead>detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
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
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Pagination
        offset={page.offset}
        limit={page.limit}
        total={page.totalCount}
        onOffset={onOffset}
        onLimit={onLimit}
      />
    </div>
  );
}
