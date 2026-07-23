"use client";

import { useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { QueryErrorState } from "@/components/query-error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { UnmanagedDatasetTable } from "@/components/ingestion/unmanaged-dataset-table";
import { PageHeader } from "@/components/page-header";
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
      <PageHeader title="Unmanaged datasets" />

      <p className="text-sm text-muted-foreground">
        DataHub datasets covered by no ingestion source — the &quot;what is being
        ingested in an unmanaged way?&quot; answer. The registry is refreshed
        hourly. This view is read-only.
      </p>

      {error && (
        <QueryErrorState error={error} context="Failed to load unmanaged datasets" />
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
