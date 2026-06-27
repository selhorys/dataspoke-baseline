"use client";

import { useState } from "react";
import Link from "next/link";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { IngestionSourceList } from "@/components/ingestion/ingestion-source-list";
import { useIngestionSources } from "@/lib/api/ingestion";
import { useMe } from "@/lib/auth/use-me";
import { filterKeyToQuery } from "@/lib/ingestion-mode-variant";
import { DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { PageHeader } from "@/components/page-header";
import type { IngestionFilterKey } from "@/types/ingestion";

export default function IngestionConfPage() {
  const { canWrite } = useMe();
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [filterKey, setFilterKey] = useState<IngestionFilterKey>("ALL");

  const { mode } = filterKeyToQuery(filterKey);

  const { data, isLoading, error } = useIngestionSources({
    offset,
    limit,
    mode,
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="Ingestion"
        actions={
          canWrite && (
            <Button size="sm" asChild>
              <Link href="/ingestion/sources/new">
                <Plus className="mr-2 h-4 w-4" />
                Create source
              </Link>
            </Button>
          )
        }
      />

      {error && (
        <ErrorState message={`Failed to load ingestion sources: ${error.message}`} />
      )}

      <IngestionSourceList
        sources={data?.sources ?? []}
        isLoading={isLoading}
        filterKey={filterKey}
        onFilterKeyChange={(k) => {
          setFilterKey(k);
          setOffset(0);
        }}
        page={{
          offset,
          limit,
          totalCount: data?.total_count ?? 0,
        }}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
