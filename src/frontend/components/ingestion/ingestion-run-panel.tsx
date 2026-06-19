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

      {lastRun && <RunSummary lastRun={lastRun} />}
    </div>
  );
}

/**
 * Readable summary of a completed run's `detail`: discovered vs emitted counts,
 * the discovered ("would emit") URN plan, and any errors/warnings. Guards every
 * field with a nullish fallback so older detail payloads do not crash the panel.
 */
function RunSummary({ lastRun }: { lastRun: IngestionRunResponse }) {
  const detail = lastRun.detail;
  const dryRun = detail?.dry_run === true;
  const discoveredCount = detail?.discovered_urns_count ?? 0;
  const emittedCount = detail?.emitted_urns_count ?? 0;
  const discoveredUrns = detail?.discovered_urns ?? [];

  return (
    <div className="rounded-md border bg-muted/30 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={eventStatusVariant(lastRun.status)} className="text-xs">
          {lastRun.status}
        </Badge>
        <span className="font-mono text-xs text-muted-foreground">
          run_id {lastRun.run_id}
        </span>
      </div>

      <p className="mt-2 text-sm">
        {dryRun ? (
          <>
            Discovered {discoveredCount} · no datasets emitted (dry run)
          </>
        ) : (
          <>
            Discovered {discoveredCount} · Emitted {emittedCount}
          </>
        )}
      </p>

      {discoveredUrns.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Discovered datasets ({discoveredUrns.length})
          </summary>
          <ul className="mt-1 max-h-48 space-y-0.5 overflow-auto">
            {discoveredUrns.map((urn) => (
              <li key={urn} className="font-mono text-xs text-muted-foreground">
                {urn}
              </li>
            ))}
          </ul>
        </details>
      )}

      <DetailMessages label="Errors" value={detail?.errors} />
      <DetailMessages label="Warnings" value={detail?.warnings} />
    </div>
  );
}

/** Renders `errors`/`warnings` (string, array, or object) when non-empty. */
function DetailMessages({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  if (Array.isArray(value) && value.length === 0) return null;
  if (
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value as object).length === 0
  ) {
    return null;
  }
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);

  return (
    <div className="mt-2">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="mt-0.5 overflow-auto whitespace-pre-wrap font-mono text-xs text-muted-foreground">
        {text}
      </pre>
    </div>
  );
}
