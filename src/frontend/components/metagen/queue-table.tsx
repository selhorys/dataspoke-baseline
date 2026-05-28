"use client";

/**
 * QueueTable — cross-dataset MetaGen item queue with filters.
 * Columns: dataset_urn (link), kind, status, candidate_count.
 */

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useMetagenQueue } from "@/lib/api/metagen";

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  pending: "outline",
  llm_approved: "secondary",
  approved: "default",
};

export function QueueTable() {
  const [datasetUrnFilter, setDatasetUrnFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 20;

  const { data, isLoading } = useMetagenQueue({
    dataset_urn: datasetUrnFilter || undefined,
    kind: kindFilter || undefined,
    status: statusFilter || undefined,
    offset,
    limit,
  });

  const total = data?.total_count ?? 0;
  const hasNext = offset + limit < total;
  const hasPrev = offset > 0;

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
          value={kindFilter}
          onValueChange={(v) => {
            setKindFilter(v === "all" ? "" : v);
            setOffset(0);
          }}
        >
          <SelectTrigger className="h-8 w-48 text-xs">
            <SelectValue placeholder="All kinds" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All kinds</SelectItem>
            <SelectItem value="dataset.description">dataset.description</SelectItem>
            <SelectItem value="column.description">column.description</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={statusFilter}
          onValueChange={(v) => {
            setStatusFilter(v === "all" ? "" : v);
            setOffset(0);
          }}
        >
          <SelectTrigger className="h-8 w-40 text-xs">
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="pending">pending</SelectItem>
            <SelectItem value="llm_approved">llm_approved</SelectItem>
            <SelectItem value="approved">approved</SelectItem>
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
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.items.map((item) => (
                <tr key={item.composite_id} className="hover:bg-muted/30">
                  <td className="px-3 py-2">
                    <Link
                      href={`/metagen/data/${encodeURIComponent(item.dataset_urn)}`}
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {total > limit && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setOffset((o) => Math.max(0, o - limit))}
              disabled={!hasPrev}
            >
              Prev
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setOffset((o) => o + limit)}
              disabled={!hasNext}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
