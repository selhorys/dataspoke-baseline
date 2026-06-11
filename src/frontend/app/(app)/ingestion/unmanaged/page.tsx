"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { UnmanagedDatasetTable } from "@/components/ingestion/unmanaged-dataset-table";
import { useIngestionUnmanaged } from "@/lib/api/ingestion";

const PAGE_SIZE = 50;

export default function UnmanagedDatasetsPage() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, error } = useIngestionUnmanaged({
    offset,
    limit: PAGE_SIZE,
  });

  const totalCount = data?.total_count ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/ingestion"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to ingestion list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">
          Unmanaged datasets
        </h1>
      </div>

      <p className="max-w-2xl text-sm text-muted-foreground">
        DataHub datasets covered by no ingestion source — the &quot;what is being
        ingested in an unmanaged way?&quot; answer. The registry is refreshed
        hourly. This view is read-only.
      </p>

      {error && (
        <ErrorState message={`Failed to load unmanaged datasets: ${error.message}`} />
      )}

      {isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : (
        <UnmanagedDatasetTable datasetUrns={data?.dataset_urns ?? []} />
      )}

      {totalCount > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {currentPage} of {totalPages} ({totalCount} total)
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
              disabled={offset + PAGE_SIZE >= totalCount}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
