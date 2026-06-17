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
import type { IngestionFilterKey } from "@/types/ingestion";

const PAGE_SIZE = 20;

export default function IngestionConfPage() {
  const { canWrite } = useMe();
  const [offset, setOffset] = useState(0);
  const [filterKey, setFilterKey] = useState<IngestionFilterKey>("ALL");

  const { mode, adHoc } = filterKeyToQuery(filterKey);

  const { data, isLoading, error } = useIngestionSources({
    offset,
    limit: PAGE_SIZE,
    mode,
    adHoc,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Ingestion</h1>
        <div className="flex items-center gap-2">
          {canWrite && (
            <Button size="sm" asChild>
              <Link href="/ingestion/sources/new">
                <Plus className="mr-2 h-4 w-4" />
                Create source
              </Link>
            </Button>
          )}
        </div>
      </div>

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
          limit: PAGE_SIZE,
          totalCount: data?.total_count ?? 0,
        }}
        onPrev={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        onNext={() => setOffset(offset + PAGE_SIZE)}
      />
    </div>
  );
}
