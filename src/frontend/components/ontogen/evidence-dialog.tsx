"use client";

/**
 * EvidenceDialog — an "Evidence" button (rendered in the Confidence cell) that
 * opens a modal lazily fetching GET /spoke/ontogen/result/{kind}/{id}/attr and
 * rendering its `evidence` JSON (the adversarial-debate transcript) as-is.
 * Read-only; available for any row regardless of role or status.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useOntogenItemAttr } from "@/lib/api/ontogen";
import type { ReviewKind } from "@/lib/api/ontogen";

interface EvidenceDialogProps {
  kind: ReviewKind;
  id: string;
}

export function EvidenceDialog({ kind, id }: EvidenceDialogProps) {
  const [open, setOpen] = useState(false);
  const { data, isLoading, error } = useOntogenItemAttr(kind, id, open);

  const evidence = data?.evidence ?? {};
  const isEmpty = Object.keys(evidence).length === 0;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-6 px-1.5 text-xs"
        onClick={() => setOpen(true)}
      >
        Evidence
      </Button>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Evidence</DialogTitle>
        </DialogHeader>
        {isLoading && <p className="text-xs text-muted-foreground">Loading evidence…</p>}
        {!isLoading && error && (
          <p className="text-xs text-destructive">Failed to load evidence: {error.message}</p>
        )}
        {!isLoading && !error && isEmpty && (
          <p className="text-xs text-muted-foreground">No evidence recorded.</p>
        )}
        {!isLoading && !error && !isEmpty && (
          <pre className="max-h-[60vh] overflow-auto rounded bg-muted p-3 text-xs">
            {JSON.stringify(evidence, null, 2)}
          </pre>
        )}
      </DialogContent>
    </Dialog>
  );
}
