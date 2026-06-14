"use client";

/**
 * IngestionEventTable — run/event history for a source, newest first.
 *
 * Columns: occurred_at, status badge (via eventStatusVariant), event_type,
 * detail (mono JSON). Includes from/to datetime-local filters and pagination.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Events.
 */

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
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
import type { IngestionEvent } from "@/types/ingestion";

interface IngestionEventTableProps {
  events: IngestionEvent[];
  /** datetime-local string ("YYYY-MM-DDTHH:MM") or "". */
  from: string;
  to: string;
  onFromChange: (v: string) => void;
  onToChange: (v: string) => void;
  page: { offset: number; limit: number; totalCount: number };
  onPrev: () => void;
  onNext: () => void;
}

export function IngestionEventTable({
  events,
  from,
  to,
  onFromChange,
  onToChange,
  page,
  onPrev,
  onNext,
}: IngestionEventTableProps) {
  const totalPages = Math.max(1, Math.ceil(page.totalCount / page.limit));
  const currentPage = Math.floor(page.offset / page.limit) + 1;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          from
          <Input
            type="datetime-local"
            value={from}
            onChange={(e) => onFromChange(e.target.value)}
            className="w-auto min-w-[15rem]"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          to
          <Input
            type="datetime-local"
            value={to}
            onChange={(e) => onToChange(e.target.value)}
            className="w-auto min-w-[15rem]"
          />
        </label>
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
