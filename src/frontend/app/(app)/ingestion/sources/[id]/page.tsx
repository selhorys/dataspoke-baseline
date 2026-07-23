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
import { QueryErrorState } from "@/components/query-error-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { RecipeYamlEditor } from "@/components/ingestion/recipe-yaml-editor";
import { SecretRefAuthoringGuide } from "@/components/ingestion/secret-ref-authoring-guide";
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
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { useDisplayLinks } from "@/lib/api/peripheral-links";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { toast } from "@/components/ui/use-toast";
import type { IngestionSourceBody } from "@/types/ingestion";

const RECIPE_FORM_ID = "ingestion-recipe-form";

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
  const [datasetLimit, setDatasetLimit] = useState(DEFAULT_PAGE_SIZE);
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);
  // Persisted selection; resolving via useMemo keeps the events query key
  // stable until the selection changes.
  const tz = useDisplayTz();
  // From GET /spoke/common/peripheral-links; safety-checked there.
  const { datahubUrl } = useDisplayLinks();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.ingestionSourceEvents,
  );
  const range = useMemo(() => resolveRange(sel, "datetime", tz), [sel, tz]);

  const { data: source, isLoading, error } = useIngestionSource(id);

  const { data: datasets } = useIngestionSourceDatasets(id, {
    offset: datasetOffset,
    limit: datasetLimit,
  });

  const { data: events } = useIngestionSourceEvents(id, {
    offset: eventOffset,
    limit: eventLimit,
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
        <QueryErrorState
          error={error}
          context="Failed to load ingestion source"
          message={
            is404
              ? "Ingestion source not found."
              : `Failed to load ingestion source: ${error?.message ?? "unknown error"}`
          }
        />
        <Button variant="outline" size="sm" asChild>
          <Link href="/ingestion/conf">Back to list</Link>
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
        router.push("/ingestion/conf");
      },
    });
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/ingestion/conf"
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
        {source.mode === "ACTIVE_CUSTOM_MANAGED" ? (
          <ScheduleTierLink
            tier={scheduleTierLabel(source.schedule)}
            dagId={scheduleDagId(
              "ingestion-active",
              scheduleTierLabel(source.schedule),
            )}
            className="text-sm text-muted-foreground"
          />
        ) : (
          <span className="text-sm text-muted-foreground">delegated</span>
        )}
        {source.datahub_source_urn &&
          (datahubUrl ? (
            <a
              href={`${datahubUrl}/ingestion/sources?hideSystem=true`}
              target="_blank"
              rel="noopener noreferrer"
              className="truncate font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
            >
              {source.datahub_source_urn}
            </a>
          ) : (
            <span className="truncate font-mono text-xs text-muted-foreground">
              {source.datahub_source_urn}
            </span>
          ))}
      </div>

      {/* 1. Recipe */}
      <section className="rounded-lg border p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium">Recipe (YAML)</h2>
          {/* Distinct React keys keep Edit/Save from sharing a node slot; without
              them React reuses the button and the Edit click submits the form on
              first render (project_frontend_button_submit_morph). */}
          {isEditable &&
            (isEditing ? (
              <div className="flex gap-2">
                <Button
                  key="recipe-save"
                  type="submit"
                  form={RECIPE_FORM_ID}
                  size="sm"
                  disabled={replace.isPending}
                >
                  {replace.isPending ? "Saving…" : "Save"}
                </Button>
                <Button
                  key="recipe-cancel"
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setIsEditing(false)}
                  disabled={replace.isPending}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              <div className="flex gap-2">
                <Button
                  key="recipe-edit"
                  variant="outline"
                  size="sm"
                  onClick={() => setIsEditing(true)}
                >
                  Edit
                </Button>
                <Button
                  key="recipe-delete"
                  variant="destructive"
                  size="sm"
                  onClick={() => setShowDeleteDialog(true)}
                >
                  Delete
                </Button>
              </div>
            ))}
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
          formId={RECIPE_FORM_ID}
          hideActions
          onCancel={() => setIsEditing(false)}
          onSave={handleSave}
          isSaving={replace.isPending}
          serverError={saveError}
          secretRefGuide={
            source.mode === "ACTIVE_CUSTOM_MANAGED" ? (
              <SecretRefAuthoringGuide />
            ) : undefined
          }
        />
      </section>

      {/* 2. Datasets */}
      <section className="space-y-3 rounded-lg border p-5">
        <h2 className="text-sm font-medium">Datasets</h2>
        <SourceDatasetTable rows={datasets?.datasets ?? []} />
        <Pagination
          offset={datasetOffset}
          limit={datasetLimit}
          total={datasets?.total_count ?? 0}
          onOffset={setDatasetOffset}
          onLimit={setDatasetLimit}
        />
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
            limit: eventLimit,
            totalCount: events?.total_count ?? 0,
          }}
          onOffset={setEventOffset}
          onLimit={setEventLimit}
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
