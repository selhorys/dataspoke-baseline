"use client";

/**
 * Dataset catalog — /governance/datasets.
 *
 * A cross-feature list of every registered dataset, consuming GET /spoke/common/data.
 * Columns: dataset_urn (→ per-dataset hub), datahub (external deep-link), ingestion
 * (owning source → /ingestion/sources/[id]), metagen (matching confs → /metagen/conf/[id]).
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Datasets.
 */

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { PageHeader } from "@/components/page-header";
import { DatahubDatasetLink } from "@/components/datahub-dataset-link";
import { modeBadgeVariant, modeLabel } from "@/lib/ingestion-mode-variant";
import { useDatasetList } from "@/lib/api/datasets";

const EM_DASH = <span className="text-muted-foreground">—</span>;

export default function GovernanceDatasetsPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);

  const { data, isLoading, error } = useDatasetList({ offset, limit });

  const rows = data?.datasets ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Datasets" />

      {error && <ErrorState message={`Failed to load datasets: ${error.message}`} />}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>dataset_urn</TableHead>
              <TableHead>datahub</TableHead>
              <TableHead>ingestion</TableHead>
              <TableHead>metagen</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 4 }).map((__, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            {!isLoading && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-8 text-center text-muted-foreground">
                  No registered datasets found.
                </TableCell>
              </TableRow>
            )}
            {rows.map((row) => (
              <TableRow key={row.dataset_urn} className="hover:bg-muted/50">
                <TableCell>
                  <Link
                    href={`/data/${encodeURIComponent(row.dataset_urn)}`}
                    className="font-mono text-xs hover:underline"
                  >
                    {row.dataset_urn}
                  </Link>
                </TableCell>
                <TableCell>
                  <DatahubDatasetLink urn={row.dataset_urn} fallback={EM_DASH} />
                </TableCell>
                <TableCell className="text-sm">
                  {row.ingestion ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <Link
                        href={`/ingestion/sources/${encodeURIComponent(row.ingestion.source_id)}`}
                        className="font-medium hover:underline"
                      >
                        {row.ingestion.name}
                      </Link>
                      <Badge variant={modeBadgeVariant(row.ingestion.mode)} className="text-xs">
                        {modeLabel(row.ingestion.mode)}
                      </Badge>
                    </span>
                  ) : (
                    EM_DASH
                  )}
                </TableCell>
                <TableCell className="text-sm">
                  {row.metagen.length > 0 ? (
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      {row.metagen.map((conf) => (
                        <Link
                          key={conf.conf_id}
                          href={`/metagen/conf/${encodeURIComponent(conf.conf_id)}`}
                          className="hover:underline"
                        >
                          {conf.name}
                        </Link>
                      ))}
                    </span>
                  ) : (
                    EM_DASH
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <Pagination
        offset={offset}
        limit={limit}
        total={data?.total_count ?? 0}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
