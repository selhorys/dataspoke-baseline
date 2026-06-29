"use client";

/**
 * MetagenUncoveredTable — registered datasets reached by no enabled conf.
 *
 * Each row carries a `reason` (no_conf_match / boundary_blocked) and links to
 * its unified per-dataset hub (/data/[urn]). Read-only.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Uncovered.
 */

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { DatahubDatasetLink } from "@/components/datahub-dataset-link";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { MetagenUncoveredRow } from "@/types/metagen";

const REASON_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  no_conf_match: "secondary",
  boundary_blocked: "outline",
};

interface MetagenUncoveredTableProps {
  rows: MetagenUncoveredRow[];
}

export function MetagenUncoveredTable({ rows }: MetagenUncoveredTableProps) {
  if (rows.length === 0) {
    return (
      <EmptyState message="No uncovered datasets — every registered dataset is reached by a conf." />
    );
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>dataset_urn</TableHead>
            <TableHead>datahub</TableHead>
            <TableHead>reason</TableHead>
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
                <DatahubDatasetLink
                  urn={row.dataset_urn}
                  fallback={<span className="text-muted-foreground">—</span>}
                />
              </TableCell>
              <TableCell>
                <Badge variant={REASON_VARIANT[row.reason] ?? "outline"} className="text-xs">
                  {row.reason}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
