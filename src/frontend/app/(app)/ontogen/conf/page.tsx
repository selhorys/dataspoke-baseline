"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OntogenConfForm } from "@/components/ontogen/conf-form";
import { OntogenConfView } from "@/components/ontogen/conf-view";
import { RunDialog } from "@/components/ontogen/run-dialog";
import {
  useOntogenConf,
  useUpsertOntogenConf,
  useRunOntogen,
} from "@/lib/api/ontogen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import { QueryErrorState } from "@/components/query-error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { datasetFilterError } from "@/lib/dataset-filter-error";
import type { OntogenConfPutBody } from "@/types/ontogen";

const CONF_FORM_ID = "ontogen-conf-form";

export default function OntogenConfPage() {
  const { canWrite } = useMe();
  const { data: conf, isLoading, error } = useOntogenConf();
  const upsertMutation = useUpsertOntogenConf();
  const runMutation = useRunOntogen();
  const { toast } = useToast();

  const [editing, setEditing] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [datasetFilter, setDatasetFilter] = useState<string>("");
  // Bumped on Cancel to remount the form, discarding dirty react-hook-form fields
  // (the form only re-syncs from `conf` on mount / when `conf` changes).
  const [formNonce, setFormNonce] = useState(0);

  useEffect(() => {
    if (conf) {
      setDatasetFilter(conf.dataset_filter ?? "");
    }
  }, [conf]);

  function handleSubmit(body: OntogenConfPutBody) {
    upsertMutation.mutate(body, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: "Configuration saved" });
      },
      onError: (err) => {
        toast({ title: "Save failed", description: err.message, variant: "destructive" });
      },
    });
  }

  function handleCancel() {
    setEditing(false);
    setDatasetFilter(conf?.dataset_filter ?? "");
    // Discard dirty form fields (is_enabled / schedule_tier / default_run_prompt)
    // by remounting the form so it re-initializes from the saved conf.
    setFormNonce((n) => n + 1);
  }

  function handleRun(params: { promptMd?: string; dry_run: boolean }) {
    runMutation.mutate(params, {
      onSuccess: (data) => {
        setRunDialogOpen(false);
        const label = data.dry_run ? "Dry run complete" : "Run complete";
        const detail = Object.entries(data.counts)
          .map(([k, v]) => `${k}: ${v}`)
          .join(", ");
        toast({
          title: label,
          description: detail || data.status,
        });
      },
      onError: (err) => {
        toast({ title: "Run failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">OntoGen — Configuration</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Singleton operational conf for the ontology inference DAG.
          </p>
        </div>
        {canWrite && (
          <div className="flex gap-2">
            {editing ? (
              <>
                <Button
                  key="conf-save"
                  type="submit"
                  form={CONF_FORM_ID}
                  disabled={upsertMutation.isPending}
                >
                  {upsertMutation.isPending ? "Saving…" : "Save"}
                </Button>
                <Button
                  key="conf-cancel"
                  type="button"
                  variant="outline"
                  onClick={handleCancel}
                  disabled={upsertMutation.isPending}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <>
                <Button key="conf-edit" type="button" variant="outline" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button
                  key="conf-run"
                  type="button"
                  onClick={() => setRunDialogOpen(true)}
                  disabled={runMutation.isPending}
                >
                  {runMutation.isPending ? "Running…" : "Run"}
                </Button>
              </>
            )}
          </div>
        )}
      </div>

      {isLoading && (
        <div className="space-y-3">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-3/4" />
        </div>
      )}

      {!isLoading && error && (
        <QueryErrorState error={error} context="Failed to load configuration" />
      )}

      {!isLoading && !error && conf === undefined && (
        <EmptyState message="No configuration found." />
      )}

      {!isLoading && !error && conf && (
        editing ? (
          <OntogenConfForm
            key={formNonce}
            formId={CONF_FORM_ID}
            initialValues={conf}
            datasetFilter={datasetFilter}
            onDatasetFilterChange={setDatasetFilter}
            datasetFilterError={datasetFilterError(upsertMutation.error)}
            onSubmit={handleSubmit}
          />
        ) : (
          <OntogenConfView conf={conf} datasetFilter={datasetFilter} />
        )
      )}

      <RunDialog
        open={runDialogOpen}
        onOpenChange={setRunDialogOpen}
        onRun={handleRun}
        isRunning={runMutation.isPending}
      />
    </div>
  );
}
