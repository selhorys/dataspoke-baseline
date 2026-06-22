"use client";

/**
 * MetagenDataPanel — the MetaGen body for the unified /data/[urn] page: the
 * boundary conf (read-only / edit / create) plus the candidate items rendered as
 * two foldable per-kind tables (dataset.description, column.description). Each
 * table's rows are candidates; the review flow is keyed per
 * (dataset_urn, item_id, candidate_id). The per-dataset metagen event list lives
 * in the shared Events panel.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Per-dataset (moved to /data/[urn]).
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { CollapsiblePanel } from "@/components/collapsible-panel";
import { BoundaryForm } from "@/components/metagen/boundary-form";
import { ItemKindTable } from "@/components/metagen/item-kind-table";
import {
  useMetagenBoundary,
  useUpsertMetagenBoundary,
  useDeleteMetagenBoundary,
  useMetagenItems,
} from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import type { MetagenBoundaryPutBody } from "@/types/metagen";

const BOUNDARY_FORM_ID = "metagen-boundary-form";

interface MetagenDataPanelProps {
  datasetUrn: string;
}

export function MetagenDataPanel({ datasetUrn }: MetagenDataPanelProps) {
  const { canWrite } = useMe();
  const { toast } = useToast();

  const [editingBoundary, setEditingBoundary] = useState(false);
  const [deleteBoundaryOpen, setDeleteBoundaryOpen] = useState(false);

  const { data: boundary, isLoading: boundaryLoading } =
    useMetagenBoundary(datasetUrn);
  const { data: itemsData, isLoading: itemsLoading } =
    useMetagenItems(datasetUrn);

  const upsertBoundary = useUpsertMetagenBoundary(datasetUrn);
  const deleteBoundary = useDeleteMetagenBoundary(datasetUrn);

  function handleSaveBoundary(body: MetagenBoundaryPutBody) {
    upsertBoundary.mutate(body, {
      onSuccess: () => {
        setEditingBoundary(false);
        toast({ title: "Boundary saved" });
      },
      onError: (err) => {
        toast({
          title: "Save failed",
          description: err.message,
          variant: "destructive",
        });
      },
    });
  }

  function handleDeleteBoundary() {
    deleteBoundary.mutate(undefined, {
      onSuccess: () => {
        setDeleteBoundaryOpen(false);
        toast({ title: "Boundary deleted" });
      },
      onError: (err) => {
        toast({
          title: "Delete failed",
          description: err.message,
          variant: "destructive",
        });
      },
    });
  }

  const items = itemsData?.items ?? [];
  const datasetDescItems = items.filter((i) => i.kind === "dataset.description");
  const columnDescItems = items.filter((i) => i.kind === "column.description");

  return (
    <div className="space-y-6">
      {/* ── Boundary (per-dataset conf) ──────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">attr/metagen/boundary</h3>
          {canWrite && !boundaryLoading && (
            <div className="flex items-center gap-2">
              {boundary === null && (
                <Button
                  key="boundary-create"
                  type="submit"
                  form={BOUNDARY_FORM_ID}
                  size="sm"
                  disabled={upsertBoundary.isPending}
                >
                  {upsertBoundary.isPending ? "Saving…" : "Save boundary"}
                </Button>
              )}
              {boundary !== null &&
                boundary !== undefined &&
                !editingBoundary && (
                  <>
                    <Button
                      key="boundary-edit"
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setEditingBoundary(true)}
                    >
                      Edit
                    </Button>
                    <Button
                      key="boundary-delete"
                      type="button"
                      variant="destructive"
                      size="sm"
                      onClick={() => setDeleteBoundaryOpen(true)}
                      disabled={deleteBoundary.isPending}
                    >
                      Delete
                    </Button>
                  </>
                )}
              {boundary !== null && boundary !== undefined && editingBoundary && (
                <>
                  <Button
                    key="boundary-cancel"
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setEditingBoundary(false)}
                    disabled={upsertBoundary.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    key="boundary-save"
                    type="submit"
                    form={BOUNDARY_FORM_ID}
                    size="sm"
                    disabled={upsertBoundary.isPending}
                  >
                    {upsertBoundary.isPending ? "Saving…" : "Save boundary"}
                  </Button>
                </>
              )}
            </div>
          )}
        </div>

        {boundaryLoading && <Skeleton className="h-32 w-full" />}

        {!boundaryLoading && boundary === null && (
          <>
            {canWrite ? (
              <>
                <p className="text-sm text-muted-foreground">
                  No boundary configured for this dataset. Create one to include
                  it in MetaGen runs.
                </p>
                <BoundaryForm
                  formId={BOUNDARY_FORM_ID}
                  initialValues={null}
                  onSubmit={handleSaveBoundary}
                />
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                No boundary configured for this dataset.
              </p>
            )}
          </>
        )}

        {!boundaryLoading && boundary !== null && boundary !== undefined && (
          <>
            {!editingBoundary && (
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-muted-foreground">is_enabled</dt>
                  <dd>
                    <Badge variant={boundary.is_enabled ? "default" : "secondary"}>
                      {boundary.is_enabled ? "Enabled" : "Disabled"}
                    </Badge>
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">allowed</dt>
                  <dd className="flex flex-wrap gap-1">
                    {boundary.allowed.length === 0 ? (
                      <span className="text-muted-foreground">none</span>
                    ) : (
                      boundary.allowed.map((k) => (
                        <Badge
                          key={k}
                          variant="outline"
                          className="font-mono text-xs"
                        >
                          {k}
                        </Badge>
                      ))
                    )}
                  </dd>
                </div>
                {boundary.owner && (
                  <div>
                    <dt className="text-muted-foreground">owner</dt>
                    <dd className="font-mono text-xs">{boundary.owner}</dd>
                  </div>
                )}
              </dl>
            )}

            {editingBoundary && (
              <BoundaryForm
                formId={BOUNDARY_FORM_ID}
                initialValues={boundary}
                onSubmit={handleSaveBoundary}
              />
            )}
          </>
        )}
      </div>

      {/* ── Items grouped by kind ────────────────────────────────────────────── */}
      <div className="space-y-4">
        <h3 className="text-sm font-medium">attr/metagen/item</h3>

        {itemsLoading && !itemsData && (
          <div className="space-y-2">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {!itemsLoading && items.length === 0 && (
          <EmptyState message="No items yet. Run MetaGen to generate candidates for this dataset." />
        )}

        {datasetDescItems.length > 0 && (
          <CollapsiblePanel
            title={
              <span className="font-mono text-xs">dataset.description</span>
            }
          >
            <ItemKindTable
              items={datasetDescItems}
              groupByColumn={false}
              canWrite={canWrite}
            />
          </CollapsiblePanel>
        )}

        {columnDescItems.length > 0 && (
          <CollapsiblePanel
            title={
              <span className="font-mono text-xs">column.description</span>
            }
          >
            <ItemKindTable
              items={columnDescItems}
              groupByColumn
              canWrite={canWrite}
            />
          </CollapsiblePanel>
        )}
      </div>

      <ConfirmDialog
        open={deleteBoundaryOpen}
        onOpenChange={setDeleteBoundaryOpen}
        title="Delete boundary"
        description={`Remove the MetaGen boundary for "${datasetUrn}". This dataset will be excluded from future runs.`}
        confirmLabel="Delete"
        onConfirm={handleDeleteBoundary}
        loading={deleteBoundary.isPending}
      />
    </div>
  );
}
