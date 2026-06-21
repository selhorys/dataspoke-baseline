"use client";

/**
 * MetagenEventTable — a metagen event feed bound to a `…/event` route, paired
 * with a datetime RangePicker for the from/to window. Newest first.
 *
 * Columns: occurred_at, status badge, event_type, detail (truncated JSON with
 * click-to-expand pretty-JSON via the shared EventDetailCell).
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Components (MetagenEventTable).
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
import type { MetagenEvent } from "@/types/metagen";

interface MetagenEventTableProps {
  events: MetagenEvent[];
  range: RangeSelection;
  onRangeChange: (value: RangeSelection) => void;
  tz: TzMode;
  page: { offset: number; limit: number; totalCount: number };
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

export function MetagenEventTable({
  events,
  range,
  onRangeChange,
  tz,
  page,
  onOffset,
  onLimit,
}: MetagenEventTableProps) {
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
                    <Badge variant={eventStatusVariant(e.status)} className="text-xs">
                      {e.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">{e.event_type}</TableCell>
                  <TableCell>
                    <EventDetailCell detail={e.detail ?? {}} />
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
