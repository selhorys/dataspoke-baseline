"use client";

/**
 * MetagenDatasetTable — per-dataset result rollup on /metagen/result.
 *
 * One row per dataset (`GET /spoke/metagen/dataset`). Columns: dataset / boundary,
 * items, approved, rejected, candidates, last modified at. Counts are
 * candidate-level. Filters: dataset_urn (text) + conf_id (select). Setting
 * conf_id restricts rows to datasets holding a candidate from that conf and
 * scopes every count to that conf's candidates. No kind / status filters.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Result rollup.
 */

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { useMetagenDatasets } from "@/lib/api/metagen";
import type { MetagenConf } from "@/types/metagen";

interface MetagenDatasetTableProps {
  /** Confs available for the conf_id filter select. */
  confs: MetagenConf[];
}

export function MetagenDatasetTable({ confs }: MetagenDatasetTableProps) {
  const tz = useDisplayTz();
  const [datasetUrnFilter, setDatasetUrnFilter] = useState("");
  const [confIdFilter, setConfIdFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);

  const { data, isLoading } = useMetagenDatasets({
    dataset_urn: datasetUrnFilter || undefined,
    conf_id: confIdFilter || undefined,
    offset,
    limit,
  });

  const total = data?.total_count ?? 0;

  return (
    <div className="space-y-4">
      {/* Table */}
      {isLoading && !data && (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {data && data.datasets.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No datasets match the current filters.
        </p>
      )}

      {data && data.datasets.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  dataset / boundary
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
                  items
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
                  approved
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
                  rejected
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
                  candidates
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  last modified at
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.datasets.map((d) => (
                <tr key={d.dataset_urn} className="hover:bg-muted/30">
                  <td className="px-3 py-2">
                    <Link
                      href={`/data/${encodeURIComponent(d.dataset_urn)}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {d.dataset_urn}
                    </Link>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {d.allowed.length === 0 ? (
                        <span className="text-xs text-muted-foreground">none</span>
                      ) : (
                        d.allowed.map((k) => (
                          <Badge
                            key={k}
                            variant="outline"
                            className="font-mono text-xs"
                          >
                            {k}
                          </Badge>
                        ))
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">
                    {d.item_count}
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">
                    {d.approved_count}
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">
                    {d.rejected_count}
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">
                    {d.candidate_count}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">
                    {d.last_modified_at
                      ? formatDateTime(d.last_modified_at, tz)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-2">
        <Input
          className="h-8 w-64 text-xs"
          placeholder="Filter by dataset URN…"
          value={datasetUrnFilter}
          onChange={(e) => {
            setDatasetUrnFilter(e.target.value);
            setOffset(0);
          }}
        />
        <Select
          value={confIdFilter || "all"}
          onValueChange={(v) => {
            setConfIdFilter(v === "all" ? "" : v);
            setOffset(0);
          }}
        >
          <SelectTrigger className="h-8 w-48 text-xs" aria-label="Filter by conf">
            <SelectValue placeholder="All confs" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All confs</SelectItem>
            {confs.map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Pagination */}
      <Pagination
        offset={offset}
        limit={limit}
        total={total}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
