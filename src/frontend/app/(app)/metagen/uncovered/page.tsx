"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { MetagenUncoveredTable } from "@/components/metagen/uncovered-table";
import { useMetagenUncovered } from "@/lib/api/metagen";

const PAGE_SIZE = 50;

export default function MetagenUncoveredPage() {
  const [offset, setOffset] = useState(0);
  const [includeDisallowed, setIncludeDisallowed] = useState(false);

  const { data, isLoading, error } = useMetagenUncovered(includeDisallowed, {
    offset,
    limit: PAGE_SIZE,
  });

  const totalCount = data?.total_count ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

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
          include_disallowed — also show boundary-blocked datasets
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
