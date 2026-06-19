"use client";

/**
 * UnmanagedDatasetTable — DataHub datasets covered by no ingestion source.
 *
 * Read-only. Each row is a mono URN link to the dataset's unified hub
 * (/data/[urn]). Renders an EmptyState when the bucket is empty.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Unmanaged View.
 */

import Link from "next/link";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface UnmanagedDatasetTableProps {
  datasetUrns: string[];
}

export function UnmanagedDatasetTable({
  datasetUrns,
}: UnmanagedDatasetTableProps) {
  if (datasetUrns.length === 0) {
    return <EmptyState message="No unmanaged datasets — every dataset is covered by a source." />;
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>dataset_urn</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {datasetUrns.map((urn) => (
            <TableRow key={urn}>
              <TableCell>
                <Link
                  href={`/data/${encodeURIComponent(urn)}`}
                  className="font-mono text-xs hover:underline"
                >
                  {urn}
                </Link>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
