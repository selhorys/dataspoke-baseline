"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OntogenConfForm } from "@/components/ontogen/conf-form";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  useOntogenConf,
  useUpsertOntogenConf,
  useDeleteOntogenConf,
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
  const deleteMutation = useDeleteOntogenConf();
  const { toast } = useToast();

  const [editing, setEditing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
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

  function handleDelete() {
    deleteMutation.mutate(undefined, {
      onSuccess: () => {
        setDeleteOpen(false);
        toast({ title: "Configuration deleted" });
      },
      onError: (err) => {
        toast({ title: "Delete failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">OntoGen — Configuration</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Singleton operational conf for the ontology inference DAG.
          </p>
        </div>
        <Link href="/ontogen">
          <Button variant="outline" size="sm">
            Back to browser
          </Button>
        </Link>
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
          {canWrite && !editing && (
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button
                variant="destructive"
                onClick={() => setDeleteOpen(true)}
                disabled={deleteMutation.isPending}
              >
                Delete
              </Button>
            </div>
          )}

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

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete ontogen configuration"
        description="This resets the configuration to defaults. The inference DAG will be disabled. This action does not write to DataHub."
        confirmLabel="Delete"
        onConfirm={handleDelete}
        loading={deleteMutation.isPending}
      />
    </div>
  );
}
