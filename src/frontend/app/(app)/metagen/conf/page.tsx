"use client";

import { useState } from "react";
import { ErrorState } from "@/components/ui/error-state";
import { MetagenConfList } from "@/components/metagen/conf-list";
import { RunDialog } from "@/components/metagen/run-dialog";
import {
  useMetagenConfList,
  useRunMetagenConf,
} from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { ApiError } from "@/lib/api/client";
import { useToast } from "@/components/ui/use-toast";
import type { MetagenConf, MetagenRunBody } from "@/types/metagen";

const PAGE_SIZE = 20;

export default function MetagenConfListPage() {
  const { canWrite } = useMe();
  const { toast } = useToast();

  const [offset, setOffset] = useState(0);
  const [runConf, setRunConf] = useState<MetagenConf | null>(null);

  const { data, isLoading, error } = useMetagenConfList({
    offset,
    limit: PAGE_SIZE,
  });

  // A single run mutation keyed to the conf chosen in the dialog. The dialog
  // only opens when runConf is set, so the empty-string fallback is inert.
  const runMutation = useRunMetagenConf(runConf?.id ?? "");

  function handleRun(body: MetagenRunBody) {
    runMutation.mutate(body, {
      onSuccess: (result) => {
        setRunConf(null);
        const label = result.dry_run ? "Dry run complete" : "Run complete";
        const detail = Object.entries(result.counts)
          .map(([k, v]) => `${k}: ${v}`)
          .join(", ");
        toast({ title: label, description: detail || result.status });
      },
      onError: (err) => {
        const msg =
          err instanceof ApiError ? `${err.error_code}: ${err.message}` : err.message;
        toast({ title: "Run failed", description: msg, variant: "destructive" });
      },
    });
  }

  return (
    <div className="space-y-4">
      {error && (
        <ErrorState message={`Failed to load confs: ${error.message}`} />
      )}

      <MetagenConfList
        confs={data?.confs ?? []}
        isLoading={isLoading}
        canWrite={canWrite}
        onRun={(conf) => setRunConf(conf)}
        runningConfId={runMutation.isPending ? (runConf?.id ?? null) : null}
        page={{
          offset,
          limit: PAGE_SIZE,
          totalCount: data?.total_count ?? 0,
        }}
        onPrev={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        onNext={() => setOffset(offset + PAGE_SIZE)}
      />

      <RunDialog
        open={runConf !== null}
        onOpenChange={(open) => {
          if (!open) setRunConf(null);
        }}
        onRun={handleRun}
        isRunning={runMutation.isPending}
      />
    </div>
  );
}
