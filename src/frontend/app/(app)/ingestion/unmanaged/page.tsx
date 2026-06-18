"use client";

import { useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { UnmanagedDatasetTable } from "@/components/ingestion/unmanaged-dataset-table";
import { useIngestionUnmanaged } from "@/lib/api/ingestion";

export default function UnmanagedDatasetsPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const { data, isLoading, error } = useIngestionUnmanaged({
    offset,
    limit,
  });

  const totalCount = data?.total_count ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          Unmanaged datasets
        </h1>
      </div>

      <p className="text-sm text-muted-foreground">
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
