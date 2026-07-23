"use client";

/**
 * MetagenCoveredTable — the datasets this conf's dataset_filter matches
 * (covered), with their per-dataset boundary setting summarized.
 *
 * Each row links its dataset_urn to /data/[urn] and renders a boundary summary
 * (an is_enabled badge plus an `allowed` summary). A read-only
 * "Show boundary-blocked" toggle maps to the `?include_disallowed` query param:
 * off (default) shows only writable covered datasets; on additionally surfaces
 * boundary-blocked covered rows (each carrying its blocked/reason). Read-only.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Conf detail (Covered datasets table)
 * and §Components (MetagenCoveredTable).
 */

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { QueryErrorState } from "@/components/query-error-state";
import { Pagination } from "@/components/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { MetagenCoveredDatasetSummary } from "@/types/metagen";

interface MetagenCoveredTableProps {
  rows: MetagenCoveredDatasetSummary[];
  isLoading: boolean;
  error?: Error | null;
  includeDisallowed: boolean;
  onIncludeDisallowedChange: (value: boolean) => void;
  page: { offset: number; limit: number; total: number };
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

function BoundarySummary({ row }: { row: MetagenCoveredDatasetSummary }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge variant={row.is_enabled ? "default" : "secondary"} className="text-xs">
        {row.is_enabled ? "enabled" : "disabled"}
      </Badge>
      {row.allowed.length === 0 ? (
        <span className="text-xs text-muted-foreground">none allowed</span>
      ) : (
        row.allowed.map((k) => (
          <Badge key={k} variant="outline" className="font-mono text-xs">
            {k}
          </Badge>
        ))
      )}
    </div>
  );
}

export function MetagenCoveredTable({
  rows,
  isLoading,
  error,
  includeDisallowed,
  onIncludeDisallowedChange,
  page,
  onOffset,
  onLimit,
}: MetagenCoveredTableProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Checkbox
          id="covered-include-disallowed"
          checked={includeDisallowed}
          onCheckedChange={(v) => onIncludeDisallowedChange(!!v)}
        />
        <label htmlFor="covered-include-disallowed" className="cursor-pointer text-sm">
          Show boundary-blocked
        </label>
      </div>

      {error && (
        <QueryErrorState error={error} context="Failed to load covered datasets" />
      )}

      {isLoading && rows.length === 0 ? (
        <Skeleton className="h-48 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState message="No covered datasets — this conf's filter matches no registered dataset." />
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>dataset_urn</TableHead>
                <TableHead>boundary</TableHead>
                {includeDisallowed && <TableHead>reason</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.dataset_urn}>
                  <TableCell>
                    <Link
                      href={`/data/${encodeURIComponent(row.dataset_urn)}`}
                      className="font-mono text-xs hover:underline"
                    >
                      {row.dataset_urn}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <BoundarySummary row={row} />
                  </TableCell>
                  {includeDisallowed && (
                    <TableCell>
                      {row.blocked ? (
                        <Badge variant="outline" className="text-xs">
                          {row.reason ?? "blocked"}
                        </Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
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
        total={page.total}
        onOffset={onOffset}
        onLimit={onLimit}
      />
    </div>
  );
}
