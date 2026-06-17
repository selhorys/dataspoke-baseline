"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OntogenConfForm } from "@/components/ontogen/conf-form";
import { RunDialog } from "@/components/ontogen/run-dialog";
import {
  useOntogenConf,
  useUpsertOntogenConf,
  useRunOntogen,
} from "@/lib/api/ontogen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import type { DatasetFilter } from "@/types/governance";
import type { OntogenConfPutBody } from "@/types/ontogen";

export default function OntogenConfPage() {
  const { canWrite } = useMe();
  const { data: conf, isLoading, error } = useOntogenConf();
  const upsertMutation = useUpsertOntogenConf();
  const runMutation = useRunOntogen();
  const { toast } = useToast();

  const [editing, setEditing] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [datasetFilter, setDatasetFilter] = useState<DatasetFilter>({});

  useEffect(() => {
    if (conf) {
      setDatasetFilter((conf.dataset_filter as DatasetFilter) ?? {});
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
            {!editing && (
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
            )}
            <Button
              onClick={() => setRunDialogOpen(true)}
              disabled={runMutation.isPending}
            >
              {runMutation.isPending ? "Running…" : "Run"}
            </Button>
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
        <ErrorState message={`Failed to load configuration: ${error.message}`} />
      )}

      {!isLoading && !error && conf === undefined && (
        <EmptyState message="No configuration found." />
      )}

      {!isLoading && !error && conf && (
        <>
          <OntogenConfForm
            initialValues={conf}
            datasetFilter={datasetFilter}
            onDatasetFilterChange={setDatasetFilter}
            onSubmit={handleSubmit}
            isSubmitting={upsertMutation.isPending}
            disabled={!editing}
          />

          {editing && (
            <Button
              variant="outline"
              onClick={() => setEditing(false)}
              disabled={upsertMutation.isPending}
            >
              Cancel
            </Button>
          )}
        </>
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
