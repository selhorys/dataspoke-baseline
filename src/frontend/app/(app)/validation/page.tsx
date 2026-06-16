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
import { Button } from "@/components/ui/button";
import { useValidationList } from "@/lib/api/validation";
import { ErrorState } from "@/components/ui/error-state";
import { formatDateTime } from "@/lib/format-time";
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";

const PAGE_SIZE = 20;

export default function ValidationListPage() {
  const [offset, setOffset] = useState(0);
  const [showDeleted, setShowDeleted] = useState(false);

  // Default hides removed slots (removed=false). The "Show deleted" toggle omits
  // the param so the backend returns both active and removed slots.
  const { data, isLoading, error } = useValidationList({
    offset,
    limit: PAGE_SIZE,
    removed: showDeleted ? undefined : false,
  });

  const totalPages = data ? Math.ceil(data.total_count / PAGE_SIZE) : 0;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

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
                    formatDateTime(v.latest_data_time)
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
