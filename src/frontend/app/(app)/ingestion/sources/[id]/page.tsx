"use client";

import { use, useEffect, useMemo, useState } from "react";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { RecipeYamlEditor } from "@/components/ingestion/recipe-yaml-editor";
import { SourceDatasetTable } from "@/components/ingestion/source-dataset-table";
import { IngestionRunPanel } from "@/components/ingestion/ingestion-run-panel";
import { IngestionEventTable } from "@/components/ingestion/ingestion-event-table";
import { sourceBodyToYaml } from "@/components/ingestion/recipe-yaml";
import {
  useIngestionSource,
  useReplaceIngestionSource,
  useDeleteIngestionSource,
  useRunIngestionSource,
  useIngestionSourceDatasets,
  useIngestionSourceEvents,
} from "@/lib/api/ingestion";
import { useMe } from "@/lib/auth/use-me";
import { ApiError } from "@/lib/api/client";
import {
  modeBadgeVariant,
  modeLabel,
  scheduleTierLabel,
} from "@/lib/ingestion-mode-variant";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { toast } from "@/components/ui/use-toast";
import type { IngestionSourceBody } from "@/types/ingestion";

const DATASET_PAGE_SIZE = 25;
const EVENT_PAGE_SIZE = 20;

export default function IngestionSourceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { canWrite } = useMe();
  const router = useRouter();

  const [isEditing, setIsEditing] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [datasetOffset, setDatasetOffset] = useState(0);
  const [eventOffset, setEventOffset] = useState(0);
  // Persisted selection; resolving via useMemo keeps the events query key
  // stable until the selection changes.
  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.ingestionSourceEvents,
  );
  const range = useMemo(() => resolveRange(sel, "datetime", tz), [sel, tz]);

  const { data: source, isLoading, error } = useIngestionSource(id);

  const { data: datasets } = useIngestionSourceDatasets(id, {
    offset: datasetOffset,
    limit: DATASET_PAGE_SIZE,
  });

  const { data: events } = useIngestionSourceEvents(id, {
    offset: eventOffset,
    limit: EVENT_PAGE_SIZE,
    from: range.from,
    to: range.to,
  });

  const replace = useReplaceIngestionSource(id);
  const remove = useDeleteIngestionSource(id);
  const run = useRunIngestionSource(id);

  const yamlValue = useMemo(
    () => (source ? sourceBodyToYaml(source) : ""),
    [source],
  );

  // Reset event pagination when the time filter changes.
  useEffect(() => {
    setEventOffset(0);
  }, [sel]);

  const is404 =
    error instanceof ApiError && error.error_code === "INGESTION_SOURCE_NOT_FOUND";

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-72" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (error || !source) {
    return (
      <div className="space-y-2">
        <ErrorState
          message={
            is404
              ? "Ingestion source not found."
              : `Failed to load ingestion source: ${error?.message ?? "unknown error"}`
          }
        />
        <Button variant="outline" size="sm" asChild>
          <Link href="/ingestion">Back to list</Link>
        </Button>
      </div>
    );
  }

  const isEditable = canWrite && source.mode !== "DATAHUB_MANAGED";

  const saveError =
    replace.error instanceof ApiError
      ? `${replace.error.error_code}: ${replace.error.message}`
      : replace.error?.message;

  function handleSave(body: IngestionSourceBody) {
    replace.mutate(body, {
      onSuccess: () => {
        toast({ title: "Source updated" });
        setIsEditing(false);
      },
    });
  }

  function handleDelete() {
    remove.mutate(undefined, {
      onSuccess: () => {
        toast({ title: "Source deleted" });
        router.push("/ingestion");
      },
    });
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/ingestion"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to ingestion list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-lg font-semibold tracking-tight">{source.name}</h1>
        <Badge variant={modeBadgeVariant(source.mode)} className="text-xs">
          {modeLabel(source.mode)}
        </Badge>
        <Badge variant="outline" className="font-mono text-xs">
          {source.platform}
        </Badge>
        <Badge
          variant={eventStatusVariant(source.status.toLowerCase())}
          className="text-xs"
        >
          {source.status}
        </Badge>
        <span className="text-sm text-muted-foreground">
          {scheduleTierLabel(source.schedule)}
        </span>
        {source.datahub_source_urn && (
          <span className="truncate font-mono text-xs text-muted-foreground">
            {source.datahub_source_urn}
          </span>
        )}
      </div>

      {/* 1. Recipe */}
      <section className="rounded-lg border p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium">Recipe (YAML)</h2>
          {isEditable && !isEditing && (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setIsEditing(true)}>
                Edit
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => setShowDeleteDialog(true)}
              >
                Delete
              </Button>
            </div>
          )}
        </div>

        {source.mode === "DATAHUB_MANAGED" && (
          <p className="mb-3 text-xs text-muted-foreground">
            DataHub is the source of truth for this source — the recipe is
            read-only here.
          </p>
        )}

        <RecipeYamlEditor
          value={yamlValue}
          readOnly={!isEditable}
          editing={isEditing}
          onCancel={() => setIsEditing(false)}
          onSave={handleSave}
          isSaving={replace.isPending}
          serverError={saveError}
        />
      </section>

      {/* 2. Datasets */}
      <section className="rounded-lg border p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium">Datasets</h2>
          {datasets && datasets.total_count > DATASET_PAGE_SIZE && (
            <div className="flex items-center gap-2 text-sm">
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setDatasetOffset(Math.max(0, datasetOffset - DATASET_PAGE_SIZE))
                }
                disabled={datasetOffset === 0}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setDatasetOffset(datasetOffset + DATASET_PAGE_SIZE)}
                disabled={datasetOffset + DATASET_PAGE_SIZE >= datasets.total_count}
              >
                Next
              </Button>
            </div>
          )}
        </div>
        <SourceDatasetTable rows={datasets?.datasets ?? []} />
      </section>

      {/* 3. Run */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Run</h2>
        <IngestionRunPanel
          mode={source.mode}
          canWrite={canWrite}
          onRun={(dryRun) => run.mutate({ dry_run: dryRun })}
          isRunning={run.isPending}
          error={run.error}
          lastRun={run.data}
        />
      </section>

      {/* 4. Events */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Events</h2>
        <IngestionEventTable
          events={events?.events ?? []}
          range={sel}
          onRangeChange={setSel}
          tz={tz}
          page={{
            offset: eventOffset,
            limit: EVENT_PAGE_SIZE,
            totalCount: events?.total_count ?? 0,
          }}
          onPrev={() => setEventOffset(Math.max(0, eventOffset - EVENT_PAGE_SIZE))}
          onNext={() => setEventOffset(eventOffset + EVENT_PAGE_SIZE)}
        />
      </section>

      {isEditable && (
        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title="Delete ingestion source"
          description={`Remove "${source.name}" and its dataset mappings. You will be returned to the ingestion list.`}
          confirmLabel="Delete"
          onConfirm={handleDelete}
          loading={remove.isPending}
        />
      )}
    </div>
  );
}
