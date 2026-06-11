"use client";

/**
 * IngestionSourceList — paginated source table with a mode filter.
 *
 * Columns: name link, mode badge (+ read-only badge for DATAHUB_MANAGED),
 * platform, schedule tier, covered-dataset count, latest run status.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §List View.
 */

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { eventStatusVariant } from "@/lib/event-status-variant";
import {
  modeBadgeVariant,
  modeLabel,
  scheduleTierLabel,
} from "@/lib/ingestion-mode-variant";
import {
  useIngestionSourceDatasetCounts,
  useIngestionSourceLatestRuns,
} from "@/lib/api/ingestion";
import type { IngestionMode, IngestionSource } from "@/types/ingestion";

const MODE_FILTERS: { value: IngestionMode | "ALL"; label: string }[] = [
  { value: "ALL", label: "All modes" },
  { value: "ACTIVE_CUSTOM_MANAGED", label: "Active" },
  { value: "DATAHUB_MANAGED", label: "DataHub-managed" },
  { value: "PASSIVE", label: "Passive" },
];

interface IngestionSourceListProps {
  sources: IngestionSource[];
  isLoading: boolean;
  modeFilter: IngestionMode | "ALL";
  onModeFilterChange: (mode: IngestionMode | "ALL") => void;
  page: { offset: number; limit: number; totalCount: number };
  onPrev: () => void;
  onNext: () => void;
}

export function IngestionSourceList({
  sources,
  isLoading,
  modeFilter,
  onModeFilterChange,
  page,
  onPrev,
  onNext,
}: IngestionSourceListProps) {
  const ids = sources.map((s) => s.id);
  const counts = useIngestionSourceDatasetCounts(ids);
  const latestRuns = useIngestionSourceLatestRuns(ids);
  const countById: Record<string, number | undefined> = {};
  const runStatusById: Record<string, string | undefined> = {};
  sources.forEach((s, i) => {
    countById[s.id] = counts[i]?.data?.total_count;
    runStatusById[s.id] = latestRuns[i]?.data?.events[0]?.status;
  });

  const totalPages = Math.max(1, Math.ceil(page.totalCount / page.limit));
  const currentPage = Math.floor(page.offset / page.limit) + 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Select
          value={modeFilter}
          onValueChange={(v) => onModeFilterChange(v as IngestionMode | "ALL")}
        >
          <SelectTrigger className="w-[200px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MODE_FILTERS.map((m) => (
              <SelectItem key={m.value} value={m.value}>
                {m.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>name</TableHead>
              <TableHead>mode</TableHead>
              <TableHead>platform</TableHead>
              <TableHead>schedule</TableHead>
              <TableHead>datasets</TableHead>
              <TableHead>status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 6 }).map((__, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            {!isLoading && sources.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="py-8 text-center text-muted-foreground"
                >
                  No ingestion sources found.
                </TableCell>
              </TableRow>
            )}
            {sources.map((s) => {
              const runStatus = runStatusById[s.id];
              return (
                <TableRow key={s.id} className="hover:bg-muted/50">
                  <TableCell>
                    <Link
                      href={`/ingestion/sources/${encodeURIComponent(s.id)}`}
                      className="text-sm font-medium hover:underline"
                    >
                      {s.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant={modeBadgeVariant(s.mode)} className="text-xs">
                        {modeLabel(s.mode)}
                      </Badge>
                      {s.mode === "DATAHUB_MANAGED" && (
                        <Badge variant="outline" className="text-xs">
                          read-only
                        </Badge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{s.platform}</TableCell>
                  <TableCell className="text-sm">
                    {scheduleTierLabel(s.schedule)}
                  </TableCell>
                  <TableCell className="text-sm tabular-nums">
                    {countById[s.id] ?? (
                      <span className="text-muted-foreground">…</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {runStatus ? (
                      <Badge variant={eventStatusVariant(runStatus)} className="text-xs">
                        {runStatus}
                      </Badge>
                    ) : (
                      <span className="text-sm text-muted-foreground">—</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

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
