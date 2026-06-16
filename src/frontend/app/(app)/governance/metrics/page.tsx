"use client";

import { useState } from "react";
import Link from "next/link";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
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
import { useGovernanceMetrics } from "@/lib/api/governance";
import { ErrorState } from "@/components/ui/error-state";
import { useMe } from "@/lib/auth/use-me";
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricType, MetricMode } from "@/types/governance";

const PAGE_SIZE = 20;

export default function GovernanceMetricsPage() {
  const { canWrite } = useMe();
  const tz = useDisplayTz();

  const [offset, setOffset] = useState(0);
  const [filterType, setFilterType] = useState<MetricType | "">("");
  const [filterMode, setFilterMode] = useState<MetricMode | "">("");
  const [filterEnabled, setFilterEnabled] = useState<"" | "true" | "false">("");

  const { data, isLoading, error } = useGovernanceMetrics({
    offset,
    limit: PAGE_SIZE,
    metric_type: filterType || undefined,
    mode: filterMode || undefined,
    is_enabled: filterEnabled === "" ? undefined : filterEnabled === "true",
  });

  const totalPages = data ? Math.ceil(data.total_count / PAGE_SIZE) : 0;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Governance · Metrics</h1>
        {canWrite && (
          <Button asChild size="sm">
            <Link href="/governance/metrics/new">
              <Plus className="mr-1 h-4 w-4" />
              New metric
            </Link>
          </Button>
        )}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2">
        <Select
          value={filterType || "all"}
          onValueChange={(v) => {
            setFilterType(v === "all" ? "" : (v as MetricType));
            setOffset(0);
          }}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue placeholder="All types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            <SelectItem value="ingestion-freshness">ingestion-freshness</SelectItem>
            <SelectItem value="validation-score">validation-score</SelectItem>
            <SelectItem value="doc-health">doc-health</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={filterMode || "all"}
          onValueChange={(v) => {
            setFilterMode(v === "all" ? "" : (v as MetricMode));
            setOffset(0);
          }}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="All modes" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All modes</SelectItem>
            <SelectItem value="active">active</SelectItem>
            <SelectItem value="passive">passive</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={filterEnabled || "all"}
          onValueChange={(v) => {
            setFilterEnabled(v === "all" ? "" : (v as "true" | "false"));
            setOffset(0);
          }}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="All status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All status</SelectItem>
            <SelectItem value="true">Enabled</SelectItem>
            <SelectItem value="false">Disabled</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Error */}
      {error && (
        <ErrorState message={`Failed to load metrics: ${error.message}`} />
      )}

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Title</TableHead>
              <TableHead>metric_type</TableHead>
              <TableHead>mode</TableHead>
              <TableHead>schedule_tier</TableHead>
              <TableHead>Enabled</TableHead>
              <TableHead>Updated</TableHead>
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
            {!isLoading && data?.metrics.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                  No metrics found.
                </TableCell>
              </TableRow>
            )}
            {data?.metrics.map((m) => (
              <TableRow key={m.id} className="cursor-pointer hover:bg-muted/50">
                <TableCell>
                  <Link
                    href={`/governance/metrics/${m.id}`}
                    className="font-medium hover:underline"
                  >
                    {m.title}
                  </Link>
                  <p className="text-xs text-muted-foreground">{m.id}</p>
                </TableCell>
                <TableCell>
                  <Badge variant="outline" className="text-xs">
                    {m.metric_type}
                  </Badge>
                </TableCell>
                <TableCell className="text-sm">{m.mode}</TableCell>
                <TableCell className="text-sm">{m.schedule_tier ?? "on-demand"}</TableCell>
                <TableCell>
                  <Badge variant={m.is_enabled ? "default" : "secondary"}>
                    {m.is_enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {formatDate(m.updated_at, tz)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {currentPage} of {totalPages} ({data?.total_count ?? 0} total)
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!data || offset + PAGE_SIZE >= data.total_count}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
