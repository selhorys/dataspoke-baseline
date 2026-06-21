"use client";

/**
 * QueueTable — cross-dataset / cross-conf MetaGen item queue with filters.
 *
 * Filters: dataset_urn (text), kind, status, conf_id (select populated from the
 * conf list). Columns: dataset_urn (link to the owning dataset page where review
 * happens), kind, field_path, status, candidate_count, created_at.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Result queue.
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
import { useMetagenQueue } from "@/lib/api/metagen";
import type { MetagenConf } from "@/types/metagen";

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  pending: "outline",
  llm_approved: "secondary",
  approved: "default",
};

interface QueueTableProps {
  /** Confs available for the conf_id filter select. */
  confs: MetagenConf[];
}

export function QueueTable({ confs }: QueueTableProps) {
  const tz = useDisplayTz();
  const [datasetUrnFilter, setDatasetUrnFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [confIdFilter, setConfIdFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);

  const { data, isLoading } = useMetagenQueue({
    dataset_urn: datasetUrnFilter || undefined,
    kind: kindFilter || undefined,
    status: statusFilter || undefined,
    conf_id: confIdFilter || undefined,
    offset,
    limit,
  });

  const total = data?.total_count ?? 0;

  return (
    <div className="space-y-4">
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
          value={kindFilter || "all"}
          onValueChange={(v) => {
            setKindFilter(v === "all" ? "" : v);
            setOffset(0);
          }}
        >
          <SelectTrigger className="h-8 w-48 text-xs" aria-label="Filter by kind">
            <SelectValue placeholder="All kinds" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All kinds</SelectItem>
            <SelectItem value="dataset.description">dataset.description</SelectItem>
            <SelectItem value="column.description">column.description</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={statusFilter || "all"}
          onValueChange={(v) => {
            setStatusFilter(v === "all" ? "" : v);
            setOffset(0);
          }}
        >
          <SelectTrigger className="h-8 w-40 text-xs" aria-label="Filter by status">
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="pending">pending</SelectItem>
            <SelectItem value="llm_approved">llm_approved</SelectItem>
            <SelectItem value="approved">approved</SelectItem>
          </SelectContent>
        </Select>
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

      {/* Table */}
      {isLoading && !data && (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {data && data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">No items match the current filters.</p>
      )}

      {data && data.items.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  dataset_urn
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  kind
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  field_path
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  status
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
                  candidates
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                  created_at
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.items.map((item) => (
                <tr key={item.composite_id} className="hover:bg-muted/30">
                  <td className="px-3 py-2">
                    <Link
                      href={`/data/${encodeURIComponent(item.dataset_urn)}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {item.dataset_urn}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{item.kind}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {item.field_path ?? "—"}
                  </td>
                  <td className="px-3 py-2">
                    <Badge
                      variant={STATUS_VARIANT[item.status] ?? "outline"}
                      className="text-xs"
                    >
                      {item.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">
                    {item.candidate_count}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">
                    {formatDateTime(item.created_at, tz)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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
