"use client";

/**
 * MetagenConfList — paginated conf list with a "Create conf" button.
 *
 * One row per conf: name (link to detail), is_enabled badge, schedule_tier,
 * a dataset_filter summary, result_limit, and a per-row Run action.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Conf list.
 */

import Link from "next/link";
import { Plus, Play } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { summarizeDatasetFilter } from "@/lib/metagen-filter-summary";
import { Pagination } from "@/components/pagination";
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import type { MetagenConf } from "@/types/metagen";

interface MetagenConfListProps {
  confs: MetagenConf[];
  isLoading: boolean;
  canWrite: boolean;
  onRun: (conf: MetagenConf) => void;
  runningConfId: string | null;
  page: { offset: number; limit: number; totalCount: number };
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

export function MetagenConfList({
  confs,
  isLoading,
  canWrite,
  onRun,
  runningConfId,
  page,
  onOffset,
  onLimit,
}: MetagenConfListProps) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Metadata Generation</h1>
        {canWrite && (
          <Button size="sm" asChild>
            <Link href="/metagen/conf/new">
              <Plus className="mr-2 h-4 w-4" />
              Create conf
            </Link>
          </Button>
        )}
      </div>

      {isLoading && confs.length === 0 ? (
        <Skeleton className="h-48 w-full" />
      ) : confs.length === 0 ? (
        <EmptyState message="No confs yet. Create one to start generating documentation." />
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>name</TableHead>
                <TableHead>is_enabled</TableHead>
                <TableHead>schedule_tier</TableHead>
                <TableHead>dataset_filter</TableHead>
                <TableHead className="text-right">result_limit</TableHead>
                {canWrite && <TableHead className="text-right">run</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {confs.map((conf) => (
                <TableRow key={conf.id}>
                  <TableCell>
                    <Link
                      href={`/metagen/conf/${encodeURIComponent(conf.id)}`}
                      className="font-medium text-primary hover:underline"
                    >
                      {conf.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={conf.is_enabled ? "default" : "secondary"}
                      className="text-xs"
                    >
                      {conf.is_enabled ? "enabled" : "disabled"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    <ScheduleTierLink
                      tier={conf.schedule_tier ?? "manual"}
                      dagId={scheduleDagId("metagen", conf.schedule_tier)}
                    />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {summarizeDatasetFilter(conf.dataset_filter)}
                  </TableCell>
                  <TableCell className="text-right text-sm tabular-nums">
                    {conf.result_limit}
                  </TableCell>
                  {canWrite && (
                    <TableCell className="text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onClick={() => onRun(conf)}
                        disabled={runningConfId === conf.id}
                        aria-label={`Run conf ${conf.name}`}
                      >
                        <Play className="mr-1 h-3.5 w-3.5" />
                        {runningConfId === conf.id ? "Running…" : "Run"}
                      </Button>
                    </TableCell>
                  )}
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
