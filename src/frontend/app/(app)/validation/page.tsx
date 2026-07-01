"use client";

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
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
import type { ValidationCoverage } from "@/types/validation";

const EM_DASH = <span className="text-muted-foreground">—</span>;

export default function ValidationListPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [showCovered, setShowCovered] = useState(true);
  const [showUncovered, setShowUncovered] = useState(false);
  const tz = useDisplayTz();

  // Map the two checkboxes to the server-side coverage filter. With neither box
  // checked there is nothing to fetch, so the query is disabled and the table
  // renders empty.
  const coverage: ValidationCoverage | null =
    showCovered && showUncovered
      ? "both"
      : showCovered
        ? "covered"
        : showUncovered
          ? "uncovered"
          : null;

  const { data, isLoading, error } = useValidationList(
    { offset, limit, coverage: coverage ?? undefined },
    { enabled: coverage !== null },
  );

  const rows = coverage === null ? [] : (data?.validations ?? []);
  const totalCount = coverage === null ? 0 : (data?.total_count ?? 0);
  const loading = coverage !== null && isLoading;

  return (
    <div className="space-y-4">
      <PageHeader title="Validation" />

      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2">
          <Checkbox
            id="validation-covered"
            checked={showCovered}
            onCheckedChange={(v) => {
              setShowCovered(!!v);
              setOffset(0);
            }}
          />
          <label htmlFor="validation-covered" className="cursor-pointer text-sm">
            covered
          </label>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id="validation-uncovered"
            checked={showUncovered}
            onCheckedChange={(v) => {
              setShowUncovered(!!v);
              setOffset(0);
            }}
          />
          <label htmlFor="validation-uncovered" className="cursor-pointer text-sm">
            uncovered
          </label>
        </div>
      </div>

      {error && <ErrorState message={`Failed to load validation configs: ${error.message}`} />}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>dataset_urn</TableHead>
              <TableHead>description</TableHead>
              <TableHead>variables</TableHead>
              <TableHead>latest check</TableHead>
              <TableHead>Quality Score</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading &&
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 5 }).map((__, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            {!loading && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  {coverage === null
                    ? "Select a coverage filter to list datasets."
                    : "No validation configs found."}
                </TableCell>
              </TableRow>
            )}
            {rows.map((v) => (
              <TableRow key={v.dataset_urn} className="cursor-pointer hover:bg-muted/50">
                <TableCell>
                  <Link
                    href={`/data/${encodeURIComponent(v.dataset_urn)}`}
                    className="font-mono text-sm hover:underline"
                  >
                    {v.dataset_urn}
                  </Link>
                </TableCell>
                <TableCell className="max-w-[240px] truncate text-sm">
                  {v.description ?? EM_DASH}
                </TableCell>
                <TableCell className="text-sm">{v.variable_count ?? EM_DASH}</TableCell>
                <TableCell className="text-sm">
                  {v.latest_data_time ? formatDateTime(v.latest_data_time, tz) : EM_DASH}
                </TableCell>
                <TableCell className="text-sm">
                  {v.latest_score !== null ? (
                    <Badge variant={scoreBadgeVariant(v.latest_score)}>
                      {scoreLabel(v.latest_score)}
                    </Badge>
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
        total={totalCount}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
