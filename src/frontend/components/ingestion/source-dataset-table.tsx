"use client";

/**
 * SourceDatasetTable — the source→dataset mapping table.
 *
 * Each row links to the dataset's ingestion detail page and shows its `authority`
 * (high / medium) and `derivation` (emitted / pipeline_name / matched), rendered
 * together as e.g. `high (emitted)`, plus first/last seen timestamps.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Datasets.
 */

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { IngestionSourceDatasetRow } from "@/types/ingestion";

interface SourceDatasetTableProps {
  rows: IngestionSourceDatasetRow[];
}

export function SourceDatasetTable({ rows }: SourceDatasetTableProps) {
  const tz = useDisplayTz();

  if (rows.length === 0) {
    return <EmptyState message="This source maps no datasets yet." />;
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>dataset_urn</TableHead>
            <TableHead>authority</TableHead>
            <TableHead>first_seen_at</TableHead>
            <TableHead>last_seen_at</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.dataset_urn}>
              <TableCell>
                <Link
                  href={`/ingestion/data/${encodeURIComponent(row.dataset_urn)}`}
                  className="font-mono text-xs hover:underline"
                >
                  {row.dataset_urn}
                </Link>
              </TableCell>
              <TableCell>
                <Badge variant="outline" className="text-xs">
                  {`${row.authority} (${row.derivation})`}
                </Badge>
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {formatDateTime(row.first_seen_at, tz)}
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {formatDateTime(row.last_seen_at, tz)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
