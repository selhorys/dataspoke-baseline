"use client";

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
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
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";

export default function ValidationListPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [showDeleted, setShowDeleted] = useState(false);
  const tz = useDisplayTz();

  // Default hides removed slots (removed=false). The "Show deleted" toggle omits
  // the param so the backend returns both active and removed slots.
  const { data, isLoading, error } = useValidationList({
    offset,
    limit,
    removed: showDeleted ? undefined : false,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Validation</h1>
        <div className="flex items-center gap-2">
          <Checkbox
            id="show-deleted"
            checked={showDeleted}
            onCheckedChange={(checked) => {
              setShowDeleted(checked === true);
              setOffset(0);
            }}
          />
          <Label htmlFor="show-deleted" className="cursor-pointer text-sm">
            Show deleted
          </Label>
        </div>
      </div>

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
                className={`cursor-pointer hover:bg-muted/50 ${
                  v.is_removed ? "text-muted-foreground opacity-60" : ""
                }`}
              >
                <TableCell>
                  <span className="flex items-center gap-2">
                    <Link
                      href={`/validation/data/${encodeURIComponent(v.dataset_urn)}`}
                      className="font-mono text-sm hover:underline"
                    >
                      {v.dataset_urn}
                    </Link>
                    {v.is_removed && (
                      <Badge variant="outline" className="text-xs">
                        deleted
                      </Badge>
                    )}
                  </span>
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
