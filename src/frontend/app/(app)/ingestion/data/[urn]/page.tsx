"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, ExternalLink, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { IngestionConfForm } from "@/components/ingestion/ingestion-conf-form";
import { ApiError } from "@/lib/api/client";
import {
  useIngestionConf,
  useUpsertIngestionConf,
  useDeleteIngestionConf,
  useRunIngestion,
  useIngestionEvents,
} from "@/lib/api/ingestion";
import { useMe } from "@/lib/auth/use-me";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { toInternal, defaultFormValues } from "@/components/ingestion/ingestion-conf-form.schema";
import { formatDateTime } from "@/lib/format-time";
import { ErrorState } from "@/components/ui/error-state";
import type { IngestionConfigResponse } from "@/types/ingestion";
import { getRuntimeConfig } from "@/lib/runtime-config";

// ── Conf read-only view ────────────────────────────────────────────────────────

function ConfReadOnly({ conf }: { conf: IngestionConfigResponse }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
      <div>
        <dt className="text-muted-foreground">mode</dt>
        <dd>
          <Badge variant="outline">{conf.mode}</Badge>
        </dd>
      </div>
      <div>
        <dt className="text-muted-foreground">platform</dt>
        <dd>{conf.platform}</dd>
      </div>
      {conf.mode === "active-custom" && conf.schedule_tier && (
        <div>
          <dt className="text-muted-foreground">schedule_tier</dt>
          <dd>{conf.schedule_tier}</dd>
        </div>
      )}
      <div>
        <dt className="text-muted-foreground">is_enabled</dt>
        <dd>
          <Badge variant={conf.is_enabled ? "default" : "secondary"}>
            {conf.is_enabled ? "Enabled" : "Disabled"}
          </Badge>
        </dd>
      </div>
      {conf.locator && (
        <div>
          <dt className="text-muted-foreground">locator</dt>
          <dd className="font-mono text-xs">{JSON.stringify(conf.locator)}</dd>
        </div>
      )}
      <div>
        <dt className="text-muted-foreground">identifier</dt>
        <dd className="font-mono text-xs">{JSON.stringify(conf.identifier)}</dd>
      </div>
      {conf.auth && (
        <div>
          <dt className="text-muted-foreground">auth</dt>
          <dd className="font-mono text-xs">{JSON.stringify(conf.auth)}</dd>
        </div>
      )}
      {conf.workflow_dag_id && (
        <div>
          <dt className="text-muted-foreground">workflow_dag_id</dt>
          <dd className="font-mono text-xs">{conf.workflow_dag_id}</dd>
        </div>
      )}
      <div>
        <dt className="text-muted-foreground">status</dt>
        <dd>
          <Badge variant={conf.status === "OK" ? "default" : "destructive"}>
            {conf.status}
          </Badge>
        </dd>
      </div>
    </dl>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function IngestionDetailPage({
  params,
}: {
  params: Promise<{ urn: string }>;
}) {
  const { urn: datasetUrn } = use(params);
  const router = useRouter();
  const { canWrite } = useMe();
  const datahubUrl = getRuntimeConfig().datahubUrl;

  const [isEditing, setIsEditing] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showRunDialog, setShowRunDialog] = useState(false);
  const [isDryRun, setIsDryRun] = useState(false);
  const [runResult, setRunResult] = useState<{ status: string; detail: Record<string, unknown> } | null>(null);

  // ── Queries ──────────────────────────────────────────────────────────────────
  const {
    data: conf,
    isLoading: confLoading,
    error: confError,
  } = useIngestionConf(datasetUrn);

  const { data: eventsData } = useIngestionEvents(datasetUrn, 5);

  // ── Mutations ─────────────────────────────────────────────────────────────────
  const upsert = useUpsertIngestionConf(datasetUrn);
  const deleteConf = useDeleteIngestionConf(datasetUrn);
  const runIngestion = useRunIngestion(datasetUrn);

  // ── Handlers ─────────────────────────────────────────────────────────────────

  const handleSave = (body: Record<string, unknown>) => {
    upsert.mutate(body, {
      onSuccess: () => setIsEditing(false),
    });
  };

  const handleDelete = () => {
    deleteConf.mutate(undefined, {
      onSuccess: () => router.push("/ingestion"),
    });
  };

  const handleRun = () => {
    runIngestion.mutate(
      { dry_run: isDryRun },
      {
        onSuccess: (result) => {
          setShowRunDialog(false);
          setRunResult({ status: result.status, detail: result.detail });
        },
      },
    );
  };

  // ── Error messages ────────────────────────────────────────────────────────────

  const saveError =
    upsert.error instanceof ApiError
      ? `${upsert.error.error_code}: ${upsert.error.message}`
      : upsert.error?.message;

  const runError =
    runIngestion.error instanceof ApiError
      ? `${runIngestion.error.error_code}: ${runIngestion.error.message}`
      : runIngestion.error?.message;

  // ── 404 → show create form ─────────────────────────────────────────────────
  const is404 =
    confError instanceof ApiError && confError.status === 404;

  const isPassive = conf?.mode === "passive";
  const canRun = canWrite && conf?.mode === "active-custom";

  // ── Loading state ─────────────────────────────────────────────────────────────
  if (confLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  // ── Non-404 error ─────────────────────────────────────────────────────────────
  if (confError && !is404) {
    return (
      <div className="space-y-2">
        <ErrorState message={`Failed to load ingestion config: ${confError.message}`} />
        <Button variant="outline" size="sm" asChild>
          <Link href="/ingestion">Back to list</Link>
        </Button>
      </div>
    );
  }

  // ── Render ────────────────────────────────────────────────────────────────────

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
        <h1 className="truncate font-mono text-lg font-semibold tracking-tight">
          {datasetUrn}
        </h1>

        {canWrite && conf && !isEditing && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {canRun && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setIsDryRun(false);
                    setRunResult(null);
                    setShowRunDialog(true);
                  }}
                >
                  <Play className="mr-1 h-3.5 w-3.5" />
                  Run Now
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setIsDryRun(true);
                    setRunResult(null);
                    setShowRunDialog(true);
                  }}
                >
                  <Play className="mr-1 h-3.5 w-3.5" />
                  Dry Run
                </Button>
              </>
            )}
            {isPassive && (
              <>
                <Button variant="outline" size="sm" disabled title="Not applicable for passive mode">
                  Run Now
                </Button>
                <Button variant="outline" size="sm" disabled title="Not applicable for passive mode">
                  Dry Run
                </Button>
              </>
            )}
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

      {/* attr/ingestion/conf */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">attr/ingestion/conf</h2>

        {/* No config yet — show create form or empty state */}
        {is404 && (
          <>
            {canWrite ? (
              <>
                <p className="mb-4 text-sm text-muted-foreground">
                  No ingestion config exists for this dataset. Create one below.
                </p>
                <IngestionConfForm
                  defaultValues={defaultFormValues()}
                  onSubmit={handleSave}
                  isPending={upsert.isPending}
                  serverError={saveError}
                  datahubUrl={datahubUrl || undefined}
                />
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                No ingestion config for this dataset.
              </p>
            )}
          </>
        )}

        {/* Config exists — read-only or edit form */}
        {conf && !isEditing && <ConfReadOnly conf={conf} />}
        {conf && isEditing && (
          <IngestionConfForm
            defaultValues={toInternal(conf)}
            onSubmit={handleSave}
            onCancel={() => setIsEditing(false)}
            isPending={upsert.isPending}
            serverError={saveError}
            datahubUrl={datahubUrl || undefined}
          />
        )}

        {/* Passive mode DataHub note */}
        {conf && isPassive && !isEditing && (
          <div className="mt-4 flex items-center gap-2 rounded-md border border-border bg-muted/50 p-3 text-sm">
            <span className="text-muted-foreground">
              Passive — ingestion runs are configured externally.
            </span>
            {datahubUrl ? (
              <a
                href={`${datahubUrl}/ingestion`}
                target="_blank"
                rel="noopener noreferrer"
                className="ml-auto inline-flex items-center gap-1 text-primary hover:underline"
              >
                Configure ingestion in DataHub
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            ) : null}
          </div>
        )}
      </section>

      {/* run result feedback */}
      {runResult && (
        <div className="rounded-md border border-border bg-muted/50 p-3 text-sm">
          <span className="font-medium">Run result: </span>
          <Badge
            variant={
              runResult.status === "success"
                ? "default"
                : runResult.status === "error"
                  ? "destructive"
                  : "secondary"
            }
            className="text-xs"
          >
            {runResult.status}
          </Badge>
          {Object.keys(runResult.detail).length > 0 && (
            <span className="ml-2 font-mono text-xs text-muted-foreground">
              {JSON.stringify(runResult.detail)}
            </span>
          )}
        </div>
      )}

      {runError && (
        <p className="text-sm text-destructive">{runError}</p>
      )}

      {/* event/ingestion (latest 5) */}
      {(conf || !is404) && (
        <section className="rounded-lg border p-5">
          <h2 className="mb-3 text-sm font-medium">event/ingestion (latest 5)</h2>
          {!eventsData && (
            <p className="text-sm text-muted-foreground">Loading events...</p>
          )}
          {eventsData && eventsData.events.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No events yet.
              {isPassive && " (Populated when an external ingestor emits DataProcessInstance records.)"}
            </p>
          )}
          {eventsData && eventsData.events.length > 0 && (
            <ul className="space-y-2">
              {eventsData.events.map((e) => (
                <li key={e.id} className="flex flex-wrap items-start gap-3 text-sm">
                  <span className="shrink-0 text-muted-foreground">
                    {formatDateTime(e.occurred_at)}
                  </span>
                  <Badge
                    variant={eventStatusVariant(e.status)}
                    className="text-xs"
                  >
                    {e.status}
                  </Badge>
                  <span>{e.event_type}</span>
                  {e.detail && Object.keys(e.detail).length > 0 && (
                    <span className="font-mono text-xs text-muted-foreground">
                      {JSON.stringify(e.detail)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* Run confirm dialog */}
      {canRun && (
        <ConfirmDialog
          open={showRunDialog}
          onOpenChange={setShowRunDialog}
          title={isDryRun ? "Dry run ingestion" : "Run ingestion now"}
          description={
            isDryRun
              ? `Simulate the ingestion pipeline for "${datasetUrn}" without writing any data.`
              : `Trigger the ingestion pipeline for "${datasetUrn}" immediately.`
          }
          confirmLabel={runIngestion.isPending ? "Running..." : isDryRun ? "Dry Run" : "Run Now"}
          onConfirm={handleRun}
          loading={runIngestion.isPending}
        />
      )}

      {/* Delete confirm dialog */}
      {canWrite && conf && (
        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title="Delete ingestion config"
          description={`Permanently delete the ingestion config for "${datasetUrn}". This cannot be undone.`}
          confirmLabel="Delete"
          onConfirm={handleDelete}
          loading={deleteConf.isPending}
        />
      )}
    </div>
  );
}
