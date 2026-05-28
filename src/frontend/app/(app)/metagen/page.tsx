"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetagenConfForm } from "@/components/metagen/conf-form";
import { RunDialog } from "@/components/metagen/run-dialog";
import { QueueTable } from "@/components/metagen/queue-table";
import { EventsSection } from "@/components/metagen/events-section";
import {
  useMetagenConf,
  useUpsertMetagenConf,
  useDeleteMetagenConf,
  useRunMetagen,
  useMetagenEvents,
} from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import { ErrorState } from "@/components/ui/error-state";
import type { DatasetFilter } from "@/types/governance";
import type { MetagenGlobalConfPutBody } from "@/types/metagen";

export default function MetagenPage() {
  const { canWrite } = useMe();
  const { toast } = useToast();

  const { data: conf, isLoading: confLoading, error: confError } = useMetagenConf();
  const upsertConf = useUpsertMetagenConf();
  const deleteConf = useDeleteMetagenConf();
  const runMutation = useRunMetagen();
  const { data: eventsData } = useMetagenEvents(10);

  const [editing, setEditing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [datasetFilter, setDatasetFilter] = useState<DatasetFilter>({});

  // Sync dataset_filter when conf loads
  useEffect(() => {
    if (conf) {
      setDatasetFilter((conf.dataset_filter as DatasetFilter) ?? {});
    }
  }, [conf]);

  function handleSaveConf(body: MetagenGlobalConfPutBody) {
    upsertConf.mutate(body, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: "Configuration saved" });
      },
      onError: (err) => {
        toast({ title: "Save failed", description: err.message, variant: "destructive" });
      },
    });
  }

  function handleDeleteConf() {
    deleteConf.mutate(undefined, {
      onSuccess: () => {
        setDeleteOpen(false);
        toast({ title: "Configuration deleted" });
      },
      onError: (err) => {
        toast({ title: "Delete failed", description: err.message, variant: "destructive" });
      },
    });
  }

  function handleRun(body: { dataset_urns?: string[] | null; dry_run?: boolean }) {
    runMutation.mutate(body, {
      onSuccess: (data) => {
        setRunDialogOpen(false);
        const label = data.dry_run ? "Dry run complete" : "Run complete";
        const detail = Object.entries(data.counts)
          .map(([k, v]) => `${k}: ${v}`)
          .join(", ");
        toast({ title: label, description: detail || data.status });
      },
      onError: (err) => {
        toast({ title: "Run failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Metadata Generation</h1>
        {canWrite && (
          <Button onClick={() => setRunDialogOpen(true)} disabled={runMutation.isPending}>
            {runMutation.isPending ? "Running…" : "Run"}
          </Button>
        )}
      </div>

      {/* ── Global conf ──────────────────────────────────────────────────────── */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">attr/metagen/conf</h2>

        {confLoading && <Skeleton className="h-40 w-full" />}

        {!confLoading && confError && (
          <ErrorState message={`Failed to load configuration: ${confError.message}`} />
        )}

        {!confLoading && !confError && conf === null && (
          <>
            {canWrite ? (
              <>
                <p className="mb-4 text-sm text-muted-foreground">
                  No configuration exists. Create one below.
                </p>
                <MetagenConfForm
                  initialValues={null}
                  datasetFilter={datasetFilter}
                  onDatasetFilterChange={setDatasetFilter}
                  onSubmit={handleSaveConf}
                  isSubmitting={upsertConf.isPending}
                />
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                No configuration exists.
              </p>
            )}
          </>
        )}

        {!confLoading && !confError && conf !== null && conf !== undefined && (
          <>
            {canWrite && !editing && (
              <div className="mb-4 flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setDeleteOpen(true)}
                  disabled={deleteConf.isPending}
                >
                  Delete
                </Button>
              </div>
            )}

            <MetagenConfForm
              initialValues={conf}
              datasetFilter={datasetFilter}
              onDatasetFilterChange={setDatasetFilter}
              onSubmit={handleSaveConf}
              isSubmitting={upsertConf.isPending}
              disabled={!editing}
            />

            {editing && canWrite && (
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={() => setEditing(false)}
                disabled={upsertConf.isPending}
              >
                Cancel
              </Button>
            )}
          </>
        )}
      </section>

      {/* ── Cross-dataset item queue ─────────────────────────────────────────── */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">item queue (cross-dataset)</h2>
        <QueueTable />
      </section>

      {/* ── Global events ────────────────────────────────────────────────────── */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">event/metagen (latest 10)</h2>
        {!eventsData && <Skeleton className="h-20 w-full" />}
        {eventsData && (
          <EventsSection events={eventsData.events} emptyMessage="No global MetaGen events yet." />
        )}
      </section>

      {/* Dialogs */}
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete MetaGen configuration"
        description="This removes the global MetaGen conf. The inference DAG will be disabled until a new conf is created."
        confirmLabel="Delete"
        onConfirm={handleDeleteConf}
        loading={deleteConf.isPending}
      />

      <RunDialog
        open={runDialogOpen}
        onOpenChange={setRunDialogOpen}
        onRun={handleRun}
        isRunning={runMutation.isPending}
      />
    </div>
  );
}
