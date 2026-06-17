"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { SeedEditor } from "@/components/ontogen/seed-editor";
import {
  useOntogenSeeds,
  useOntogenSeed,
  useCreateSeed,
  useUpdateSeed,
  useDeleteSeed,
} from "@/lib/api/ontogen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

export default function OntogenSeedPage() {
  const { canWrite } = useMe();
  const { toast } = useToast();

  const { data: seedList, isLoading: listLoading } = useOntogenSeeds({ limit: 50 });
  const seeds = seedList?.seeds ?? [];

  // Track which seed is open in expanded view/edit mode
  const [openSeedId, setOpenSeedId] = useState<string | null>(null);
  const [editingSeedId, setEditingSeedId] = useState<string | null>(null);
  const [creatingNew, setCreatingNew] = useState(false);
  const [deleteSeedId, setDeleteSeedId] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">OntoGen — Seed Library</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Markdown seeds provide context for the ontology inference DAG.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {canWrite && !creatingNew && (
            <Button size="sm" onClick={() => { setCreatingNew(true); setOpenSeedId(null); setEditingSeedId(null); }}>
              + New Seed
            </Button>
          )}
        </div>
      </div>

      {creatingNew && (
        <NewSeedCard
          onCreated={(seedId) => {
            setCreatingNew(false);
            setOpenSeedId(seedId);
          }}
          onCancel={() => setCreatingNew(false)}
        />
      )}

      {listLoading && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}

      {!listLoading && seeds.length === 0 && !creatingNew && (
        <div className="rounded-md border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            No seeds yet. Create one to guide ontology inference.
          </p>
        </div>
      )}

      <ul className="space-y-3">
        {seeds.map((seed) => (
          <SeedListRow
            key={seed.seed_id}
            seedId={seed.seed_id}
            preview={seed.preview}
            updatedAt={seed.updated_at}
            isOpen={openSeedId === seed.seed_id}
            isEditing={editingSeedId === seed.seed_id}
            canWrite={canWrite}
            onToggle={() =>
              setOpenSeedId(openSeedId === seed.seed_id ? null : seed.seed_id)
            }
            onEdit={() => {
              setEditingSeedId(seed.seed_id);
              setOpenSeedId(seed.seed_id);
            }}
            onCancelEdit={() => setEditingSeedId(null)}
            onEditSaved={() => {
              setEditingSeedId(null);
              toast({ title: "Seed updated" });
            }}
            onDeleteRequest={() => setDeleteSeedId(seed.seed_id)}
          />
        ))}
      </ul>

      {deleteSeedId && (
        <DeleteSeedDialog
          seedId={deleteSeedId}
          onClose={() => setDeleteSeedId(null)}
          onDeleted={() => {
            setDeleteSeedId(null);
            if (openSeedId === deleteSeedId) setOpenSeedId(null);
            toast({ title: "Seed deleted" });
          }}
        />
      )}
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function NewSeedCard({
  onCreated,
  onCancel,
}: {
  onCreated: (seedId: string) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreateSeed();
  const { toast } = useToast();

  function handleSave(body: string) {
    createMutation.mutate(body, {
      onSuccess: (data) => {
        onCreated(data.seed_id);
        toast({ title: "Seed created" });
      },
      onError: (err) => {
        toast({ title: "Create failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <div className="rounded-md border p-4">
      <p className="mb-3 text-sm font-medium">New seed</p>
      <SeedEditor
        initialBody={null}
        onSave={handleSave}
        onCancel={onCancel}
        isSaving={createMutation.isPending}
      />
    </div>
  );
}

function SeedListRow({
  seedId,
  preview,
  updatedAt,
  isOpen,
  isEditing,
  canWrite,
  onToggle,
  onEdit,
  onCancelEdit,
  onEditSaved,
  onDeleteRequest,
}: {
  seedId: string;
  preview: string;
  updatedAt: string;
  isOpen: boolean;
  isEditing: boolean;
  canWrite: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onEditSaved: () => void;
  onDeleteRequest: () => void;
}) {
  const { data: body, isLoading: bodyLoading } = useOntogenSeed(isOpen ? seedId : "");
  const updateMutation = useUpdateSeed(seedId);
  const { toast } = useToast();
  const tz = useDisplayTz();

  function handleSave(newBody: string) {
    updateMutation.mutate(newBody, {
      onSuccess: () => onEditSaved(),
      onError: (err) => {
        toast({ title: "Update failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <li className="rounded-md border">
      <div
        className="flex cursor-pointer items-center justify-between px-4 py-3 hover:bg-muted/50"
        onClick={onToggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && onToggle()}
      >
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-xs text-muted-foreground">{seedId}</p>
          <p className="mt-0.5 truncate text-sm">{preview}</p>
        </div>
        <div className="ml-4 flex items-center gap-3 text-xs text-muted-foreground">
          <span>{formatDateTime(updatedAt, tz)}</span>
          {canWrite && (
            <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={onEdit}
              >
                Edit
              </Button>
              <Button
                size="sm"
                variant="destructive"
                className="h-7 text-xs"
                onClick={onDeleteRequest}
              >
                Delete
              </Button>
            </div>
          )}
          <span>{isOpen ? "▲" : "▼"}</span>
        </div>
      </div>

      {isOpen && (
        <div className="border-t px-4 py-3">
          {isEditing ? (
            <SeedEditor
              initialBody={body ?? null}
              isLoading={bodyLoading}
              onSave={handleSave}
              onCancel={onCancelEdit}
              isSaving={updateMutation.isPending}
            />
          ) : (
            <pre className="overflow-auto rounded-md bg-muted/40 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap">
              {bodyLoading ? "Loading…" : (body ?? "(empty)")}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

function DeleteSeedDialog({
  seedId,
  onClose,
  onDeleted,
}: {
  seedId: string;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const deleteMutation = useDeleteSeed(seedId);

  function handleDelete() {
    deleteMutation.mutate(undefined, {
      onSuccess: () => onDeleted(),
    });
  }

  return (
    <ConfirmDialog
      open
      onOpenChange={(open) => { if (!open) onClose(); }}
      title="Delete seed"
      description={`Delete seed ${seedId}? This cannot be undone.`}
      confirmLabel="Delete"
      onConfirm={handleDelete}
      loading={deleteMutation.isPending}
    />
  );
}
