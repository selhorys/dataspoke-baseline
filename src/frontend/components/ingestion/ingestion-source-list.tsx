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
import { Pagination } from "@/components/pagination";
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
  filterKeyLabel,
  INGESTION_FILTER_KEYS,
  scheduleTierLabel,
} from "@/lib/ingestion-mode-variant";
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import {
  useIngestionSourceDatasetCounts,
  useIngestionSourceLatestRuns,
} from "@/lib/api/ingestion";
import type { IngestionFilterKey, IngestionSource } from "@/types/ingestion";

interface IngestionSourceListProps {
  sources: IngestionSource[];
  isLoading: boolean;
  filterKey: IngestionFilterKey;
  onFilterKeyChange: (key: IngestionFilterKey) => void;
  page: { offset: number; limit: number; totalCount: number };
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

export function IngestionSourceList({
  sources,
  isLoading,
  filterKey,
  onFilterKeyChange,
  page,
  onOffset,
  onLimit,
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

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Select
          value={filterKey}
          onValueChange={(v) => onFilterKeyChange(v as IngestionFilterKey)}
        >
          <SelectTrigger className="w-[240px]" aria-label="Filter sources by mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {INGESTION_FILTER_KEYS.map((k) => (
              <SelectItem key={k} value={k}>
                {filterKeyLabel(k)}
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
                    {s.datahub_source_urn && (
                      <div className="font-mono text-xs text-muted-foreground">
                        {s.datahub_source_urn}
                      </div>
                    )}
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
                    {(() => {
                      const tier = scheduleTierLabel(s.schedule);
                      return (
                        <ScheduleTierLink
                          tier={tier}
                          dagId={scheduleDagId("ingestion-active", tier)}
                        />
                      );
                    })()}
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
