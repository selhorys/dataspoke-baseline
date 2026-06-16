"use client";

/**
 * IngestionEventTable — run/event history for a source, newest first.
 *
 * Columns: occurred_at, status badge (via eventStatusVariant), event_type,
 * detail (mono JSON). Includes a datetime range filter and pagination.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Events.
 */

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
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
  onTzChange: (tz: TzMode) => void;
  page: { offset: number; limit: number; totalCount: number };
  onPrev: () => void;
  onNext: () => void;
}

export function IngestionEventTable({
  events,
  range,
  onRangeChange,
  tz,
  onTzChange,
  page,
  onPrev,
  onNext,
}: IngestionEventTableProps) {
  const totalPages = Math.max(1, Math.ceil(page.totalCount / page.limit));
  const currentPage = Math.floor(page.offset / page.limit) + 1;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <RangePicker
          value={range}
          onChange={onRangeChange}
          tz={tz}
          onTzChange={onTzChange}
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
                    {formatDateTime(e.occurred_at)}
                  </TableCell>
                  <TableCell>
                    <Badge variant={eventStatusVariant(e.status)} className="text-xs">
                      {e.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">{e.event_type}</TableCell>
                  <TableCell className="max-w-[360px] truncate font-mono text-xs text-muted-foreground">
                    {e.detail && Object.keys(e.detail).length > 0
                      ? JSON.stringify(e.detail)
                      : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {page.totalCount > page.limit && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {currentPage} of {totalPages} ({page.totalCount} total)
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onPrev}
              disabled={page.offset === 0}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onNext}
              disabled={page.offset + page.limit >= page.totalCount}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
