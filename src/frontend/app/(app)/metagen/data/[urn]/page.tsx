"use client";

import { use, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { BoundaryForm } from "@/components/metagen/boundary-form";
import { ItemCard } from "@/components/metagen/item-card";
import { EventsSection } from "@/components/metagen/events-section";
import {
  useMetagenBoundary,
  useUpsertMetagenBoundary,
  useDeleteMetagenBoundary,
  useMetagenItems,
  useMetagenDatasetEvents,
} from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import { EmptyState } from "@/components/ui/empty-state";
import type { MetagenBoundaryPutBody } from "@/types/metagen";

export default function MetagenDatasetPage({
  params,
}: {
  params: Promise<{ urn: string }>;
}) {
  // Next.js returns the [urn] segment URL-decoded on server render but still
  // encoded after client-side navigation. Normalize to the raw URN so the API
  // client encodes exactly once — double-encoding yields a 422
  // string_pattern_mismatch with an empty error message.
  const { urn: rawUrn } = use(params);
  const datasetUrn = rawUrn.startsWith("urn:") ? rawUrn : decodeURIComponent(rawUrn);
  const { canWrite } = useMe();
  const { toast } = useToast();

  const [editingBoundary, setEditingBoundary] = useState(false);
  const [deleteBoundaryOpen, setDeleteBoundaryOpen] = useState(false);
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);

  // ── Queries ──────────────────────────────────────────────────────────────────
  const {
    data: boundary,
    isLoading: boundaryLoading,
  } = useMetagenBoundary(datasetUrn);

  const { data: itemsData, isLoading: itemsLoading } = useMetagenItems(datasetUrn);

  const { data: eventsData } = useMetagenDatasetEvents(datasetUrn, {
    offset: eventOffset,
    limit: eventLimit,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────────
  const upsertBoundary = useUpsertMetagenBoundary(datasetUrn);
  const deleteBoundary = useDeleteMetagenBoundary(datasetUrn);

  // ── Handlers ──────────────────────────────────────────────────────────────────

  function handleSaveBoundary(body: MetagenBoundaryPutBody) {
    upsertBoundary.mutate(body, {
      onSuccess: () => {
        setEditingBoundary(false);
        toast({ title: "Boundary saved" });
      },
      onError: (err) => {
        toast({ title: "Save failed", description: err.message, variant: "destructive" });
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
        toast({ title: "Delete failed", description: err.message, variant: "destructive" });
      },
    });
  }

  // ── Group items by kind ────────────────────────────────────────────────────────
  const items = itemsData?.items ?? [];
  const datasetDescItems = items.filter((i) => i.kind === "dataset.description");
  const columnDescItems = items.filter((i) => i.kind === "column.description");

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/metagen/result"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to MetaGen review queue"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="truncate font-mono text-lg font-semibold tracking-tight">
          {datasetUrn}
        </h1>
        {boundary && (
          <Badge variant={boundary.is_enabled ? "default" : "secondary"} className="text-xs">
            {boundary.is_enabled ? "enabled" : "disabled"}
          </Badge>
        )}
      </div>

      {/* ── Boundary (per-dataset conf) ──────────────────────────────────────── */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">attr/metagen/boundary</h2>

        {boundaryLoading && <Skeleton className="h-32 w-full" />}

        {!boundaryLoading && boundary === null && (
          <>
            {canWrite ? (
              <>
                <p className="mb-4 text-sm text-muted-foreground">
                  No boundary configured for this dataset. Create one to include it in MetaGen runs.
                </p>
                <BoundaryForm
                  initialValues={null}
                  onSubmit={handleSaveBoundary}
                  isSubmitting={upsertBoundary.isPending}
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
            {canWrite && !editingBoundary && (
              <div className="mb-4 flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setEditingBoundary(true)}>
                  Edit
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setDeleteBoundaryOpen(true)}
                  disabled={deleteBoundary.isPending}
                >
                  Delete
                </Button>
              </div>
            )}

            {/* Read-only view */}
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
                        <Badge key={k} variant="outline" className="font-mono text-xs">
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
              <>
                <BoundaryForm
                  initialValues={boundary}
                  onSubmit={handleSaveBoundary}
                  isSubmitting={upsertBoundary.isPending}
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => setEditingBoundary(false)}
                  disabled={upsertBoundary.isPending}
                >
                  Cancel
                </Button>
              </>
            )}
          </>
        )}
      </section>

      {/* ── Items grouped by kind ────────────────────────────────────────────── */}
      <section className="space-y-4">
        <h2 className="text-sm font-medium">attr/metagen/item</h2>

        {itemsLoading && !itemsData && (
          <div className="space-y-2">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {!itemsLoading && items.length === 0 && (
          <EmptyState message="No items yet. Run MetaGen to generate candidates for this dataset." />
        )}

        {/* dataset.description items */}
        {datasetDescItems.length > 0 && (
          <div className="space-y-3">
            <h3 className="font-mono text-xs font-medium text-muted-foreground">
              dataset.description
            </h3>
            {datasetDescItems.map((item) => (
              <ItemCard key={item.composite_id} item={item} canWrite={canWrite} />
            ))}
          </div>
        )}

        {/* column.description items */}
        {columnDescItems.length > 0 && (
          <div className="space-y-3">
            <h3 className="font-mono text-xs font-medium text-muted-foreground">
              column.description
            </h3>
            {columnDescItems.map((item) => (
              <ItemCard key={item.composite_id} item={item} canWrite={canWrite} />
            ))}
          </div>
        )}
      </section>

      {/* ── Per-dataset events ───────────────────────────────────────────────── */}
      <section className="space-y-4 rounded-lg border p-5">
        <h2 className="text-sm font-medium">event/metagen</h2>
        {!eventsData && <Skeleton className="h-20 w-full" />}
        {eventsData && (
          <EventsSection
            events={eventsData.events}
            emptyMessage="No dataset MetaGen events yet."
          />
        )}
        <Pagination
          offset={eventOffset}
          limit={eventLimit}
          total={eventsData?.total_count ?? 0}
          onOffset={setEventOffset}
          onLimit={setEventLimit}
        />
      </section>

      {/* Dialogs */}
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
