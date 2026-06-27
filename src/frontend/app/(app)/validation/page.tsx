"use client";

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
import { useValidationList } from "@/lib/api/validation";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { PageHeader } from "@/components/page-header";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";

export default function ValidationListPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const tz = useDisplayTz();

  const { data, isLoading, error } = useValidationList({ offset, limit });

  return (
    <div className="space-y-4">
      <PageHeader title="Validation" />

      {error && (
        <ErrorState message={`Failed to load validation configs: ${error.message}`} />
      )}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>dataset_urn</TableHead>
              <TableHead>description</TableHead>
              <TableHead>variables</TableHead>
              <TableHead>latest data_time</TableHead>
              <TableHead>Quality Score</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 5 }).map((__, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            {!isLoading && data?.validations.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="py-8 text-center text-muted-foreground"
                >
                  No validation configs found.
                </TableCell>
              </TableRow>
            )}
            {data?.validations.map((v) => (
              <TableRow
                key={v.dataset_urn}
                className="cursor-pointer hover:bg-muted/50"
              >
                <TableCell>
                  <Link
                    href={`/data/${encodeURIComponent(v.dataset_urn)}`}
                    className="font-mono text-sm hover:underline"
                  >
                    {v.dataset_urn}
                  </Link>
                </TableCell>
                <TableCell className="max-w-[240px] truncate text-sm">
                  {v.description}
                </TableCell>
                <TableCell className="text-sm">{v.variable_count}</TableCell>
                <TableCell className="text-sm">
                  {v.latest_data_time ? (
                    formatDateTime(v.latest_data_time, tz)
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-sm">
                  {v.latest_score !== null ? (
                    <Badge variant={scoreBadgeVariant(v.latest_score)}>
                      {scoreLabel(v.latest_score)}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground">—</span>
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
