"use client";

/**
 * IngestionRunPanel — dry-run / run trigger for a source.
 *
 * Active only for ACTIVE_CUSTOM_MANAGED sources when the caller can write.
 * Other modes show an explanatory disabled state (the run happens in DataHub or
 * externally). Server errors are mapped to human-readable messages:
 *   INGESTION_RUNNING            → a run is already in progress
 *   INGESTION_RUN_NOT_APPLICABLE → mode explanation
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Run.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { ErrorText } from "@/components/forms/error-text";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { modeDescription } from "@/lib/ingestion-mode-variant";
import { ApiError } from "@/lib/api/client";
import type { IngestionMode, IngestionRunResponse } from "@/types/ingestion";

interface IngestionRunPanelProps {
  mode: IngestionMode;
  canWrite: boolean;
  onRun: (dryRun: boolean) => void;
  isRunning: boolean;
  error: unknown;
  lastRun?: IngestionRunResponse;
}

function runErrorMessage(error: unknown, mode: IngestionMode): string | undefined {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : undefined;
  }
  if (error.error_code === "INGESTION_RUNNING") {
    return "A run is already in progress for this source.";
  }
  if (error.error_code === "INGESTION_RUN_NOT_APPLICABLE") {
    return modeDescription(mode);
  }
  return `${error.error_code}: ${error.message}`;
}

export function IngestionRunPanel({
  mode,
  canWrite,
  onRun,
  isRunning,
  error,
  lastRun,
}: IngestionRunPanelProps) {
  const [dryRun, setDryRun] = useState(false);

  const runnable = mode === "ACTIVE_CUSTOM_MANAGED";

  if (!runnable) {
    return (
      <p className="text-sm text-muted-foreground">
        Run is not available for this source. {modeDescription(mode)}
      </p>
    );
  }

  if (!canWrite) {
    return (
      <p className="text-sm text-muted-foreground">
        You need the Editor role to trigger a run.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Checkbox
            id="ingestion-dry-run"
            checked={dryRun}
            onCheckedChange={(v) => setDryRun(!!v)}
            disabled={isRunning}
          />
          <label htmlFor="ingestion-dry-run" className="cursor-pointer text-sm">
            dry_run — connection check, no writes
          </label>
        </div>
        <Button onClick={() => onRun(dryRun)} disabled={isRunning}>
          {isRunning ? "Running…" : dryRun ? "Dry Run" : "Run"}
        </Button>
      </div>

      <ErrorText message={runErrorMessage(error, mode)} />

      {lastRun && (
        <div className="rounded-md border bg-muted/30 p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={eventStatusVariant(lastRun.status)} className="text-xs">
              {lastRun.status}
            </Badge>
            <span className="font-mono text-xs text-muted-foreground">
              run_id {lastRun.run_id}
            </span>
          </div>
          {lastRun.detail && Object.keys(lastRun.detail).length > 0 && (
            <pre className="mt-2 overflow-auto whitespace-pre-wrap font-mono text-xs text-muted-foreground">
              {JSON.stringify(lastRun.detail, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
