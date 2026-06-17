"use client";

/**
 * EvidenceDisclosure — on-demand "Show evidence" toggle for a single ontogen
 * result item. When opened, lazily fetches GET /spoke/ontogen/result/{kind}/{id}/attr
 * and renders its `evidence` JSON (the adversarial-debate transcript) as-is.
 * Read-only; available for any row regardless of role or status.
 */

import { useState } from "react";
import { useOntogenItemAttr } from "@/lib/api/ontogen";
import type { ReviewKind } from "@/lib/api/ontogen";

interface EvidenceDisclosureProps {
  kind: ReviewKind;
  id: string;
}

export function EvidenceDisclosure({ kind, id }: EvidenceDisclosureProps) {
  const [open, setOpen] = useState(false);
  const { data, isLoading, error } = useOntogenItemAttr(kind, id, open);

  const evidence = data?.evidence ?? {};
  const isEmpty = Object.keys(evidence).length === 0;

  return (
    <div className="mt-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-xs text-muted-foreground underline-offset-2 hover:underline"
      >
        {open ? "Hide evidence" : "Show evidence"}
      </button>

      {open && (
        <div className="mt-1.5">
          {isLoading && <p className="text-xs text-muted-foreground">Loading evidence…</p>}
          {!isLoading && error && (
            <p className="text-xs text-destructive">Failed to load evidence: {error.message}</p>
          )}
          {!isLoading && !error && isEmpty && (
            <p className="text-xs text-muted-foreground">No evidence recorded.</p>
          )}
          {!isLoading && !error && !isEmpty && (
            <pre className="max-h-80 overflow-auto rounded bg-muted p-2 text-xs">
              {JSON.stringify(evidence, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
