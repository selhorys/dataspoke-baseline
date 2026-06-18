"use client";

import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetagenConfForm } from "@/components/metagen/conf-form";
import { RunDialog } from "@/components/metagen/run-dialog";
import { MetagenEventTable } from "@/components/metagen/metagen-event-table";
import {
  useMetagenConf,
  useUpdateMetagenConf,
  useDeleteMetagenConf,
  useRunMetagenConf,
  useMetagenConfEvents,
} from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { ApiError } from "@/lib/api/client";
import { useToast } from "@/components/ui/use-toast";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { DEFAULT_PAGE_SIZE } from "@/components/pagination";
import type { DatasetFilter } from "@/types/governance";
import type { MetagenConfPutBody, MetagenRunBody } from "@/types/metagen";

export default function MetagenConfDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { canWrite } = useMe();
  const { toast } = useToast();
  const router = useRouter();

  const [editing, setEditing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [datasetFilter, setDatasetFilter] = useState<DatasetFilter>({});
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);

  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.metagenConfEvents,
  );
  const range = useMemo(() => resolveRange(sel, "datetime", tz), [sel, tz]);

  const { data: conf, isLoading, error } = useMetagenConf(id);
  const { put } = useUpdateMetagenConf(id);
  const deleteConf = useDeleteMetagenConf(id);
  const runMutation = useRunMetagenConf(id);
  const { data: events } = useMetagenConfEvents(id, {
    from: range.from,
    to: range.to,
    offset: eventOffset,
    limit: eventLimit,
  });

  // Sync the dataset_filter editor when the conf loads.
  useEffect(() => {
    if (conf) {
      setDatasetFilter((conf.dataset_filter as DatasetFilter) ?? {});
    }
  }, [conf]);

  // Reset event pagination when the time filter changes.
  useEffect(() => {
    setEventOffset(0);
  }, [sel]);

  const is404 =
    error instanceof ApiError && error.error_code === "METAGEN_CONF_NOT_FOUND";

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-72" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error || !conf) {
    return (
      <div className="space-y-2">
        <ErrorState
          message={
            is404
              ? "Conf not found."
              : `Failed to load conf: ${error?.message ?? "unknown error"}`
          }
        />
        <Button variant="outline" size="sm" asChild>
          <Link href="/metagen/conf">Back to list</Link>
        </Button>
      </div>
    );
  }

  const saveError =
    put.error instanceof ApiError
      ? `${put.error.error_code}: ${put.error.message}`
      : put.error?.message;

  function handleSave(body: MetagenConfPutBody) {
    put.mutate(body, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: "Conf saved" });
      },
    });
  }

  function handleDelete() {
    deleteConf.mutate(undefined, {
      onSuccess: () => {
        toast({ title: "Conf deleted" });
        router.push("/metagen/conf");
      },
      onError: (err) => {
        toast({ title: "Delete failed", description: err.message, variant: "destructive" });
      },
    });
  }

  function handleRun(body: MetagenRunBody) {
    runMutation.mutate(body, {
      onSuccess: (result) => {
        setRunDialogOpen(false);
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
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/metagen/conf"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to conf list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-lg font-semibold tracking-tight">{conf.name}</h1>
        <Badge variant={conf.is_enabled ? "default" : "secondary"} className="text-xs">
          {conf.is_enabled ? "enabled" : "disabled"}
        </Badge>
        <span className="text-sm text-muted-foreground">
          {conf.schedule_tier ?? "manual"}
        </span>

        {canWrite && (
          <div className="ml-auto flex gap-2">
            {/* Distinct React keys keep Edit and Cancel from sharing a node slot;
                without them React reuses the button and the Edit click submits
                the form on first render (project_frontend_button_submit_morph). */}
            {editing ? (
              <Button
                key="conf-cancel"
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setEditing(false)}
                disabled={put.isPending}
              >
                Cancel
              </Button>
            ) : (
              <Button
                key="conf-edit"
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setEditing(true)}
              >
                Edit
              </Button>
            )}
            <Button
              type="button"
              size="sm"
              onClick={() => setRunDialogOpen(true)}
              disabled={runMutation.isPending}
            >
              {runMutation.isPending ? "Running…" : "Run"}
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => setDeleteOpen(true)}
              disabled={deleteConf.isPending}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      {/* Conf form */}
      <section className="rounded-lg border p-5">
        <MetagenConfForm
          initialValues={conf}
          datasetFilter={datasetFilter}
          onDatasetFilterChange={setDatasetFilter}
          onSubmit={handleSave}
          isSubmitting={put.isPending}
          disabled={!editing}
          serverError={editing ? saveError : undefined}
          submitLabel="Save conf"
        />
      </section>

      {/* Per-conf events */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Run events</h2>
        <MetagenEventTable
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

      {/* Dialogs */}
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete conf"
        description={`Remove "${conf.name}". Already-approved descriptions stay in DataHub; this conf's pending candidates are dropped.`}
        confirmLabel="Delete"
        onConfirm={handleDelete}
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
