"use client";

import { useState } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { MetagenUncoveredTable } from "@/components/metagen/uncovered-table";
import { useMetagenUncovered } from "@/lib/api/metagen";

export default function MetagenUncoveredPage() {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [includeDisallowed, setIncludeDisallowed] = useState(false);

  const { data, isLoading, error } = useMetagenUncovered(includeDisallowed, {
    offset,
    limit,
  });

  const totalCount = data?.total_count ?? 0;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Uncovered datasets</h1>

      <p className="text-sm text-muted-foreground">
        Registered datasets reached by no enabled conf. By default only{" "}
        <code className="font-mono">no_conf_match</code> rows are shown; enable the
        toggle to also include <code className="font-mono">boundary_blocked</code>{" "}
        datasets (matched by a conf but blocked by the per-dataset boundary). This
        view is read-only.
      </p>

      <div className="flex items-center gap-2">
        <Checkbox
          id="uncovered-include-disallowed"
          checked={includeDisallowed}
          onCheckedChange={(v) => {
            setIncludeDisallowed(!!v);
            setOffset(0);
          }}
        />
        <label
          htmlFor="uncovered-include-disallowed"
          className="cursor-pointer text-sm"
        >
          Show boundary-blocked datasets
        </label>
      </div>

      {error && (
        <ErrorState message={`Failed to load uncovered datasets: ${error.message}`} />
      )}

      {isLoading && !data ? (
        <Skeleton className="h-48 w-full" />
      ) : (
        <MetagenUncoveredTable rows={data?.datasets ?? []} />
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
