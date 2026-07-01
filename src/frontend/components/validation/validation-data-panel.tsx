"use client";

/**
 * ValidationDataPanel — the validation body for the unified /data/[urn] page:
 * the conf (read-only / edit / create) plus the score and per-variable charts.
 * The per-dataset validation event list lives in the shared Events panel.
 *
 * The conf has two states: an existing slot (200) renders read-only with
 * Edit/Delete (or an edit form), and an absent slot (404 CONFIG_NOT_FOUND)
 * renders the Create empty-state. Delete is a hard delete — afterwards the
 * dataset reads as never-created and a fresh PUT simply creates a new conf.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Detail (moved to /data/[urn]),
 *       spec/feature/VALIDATION.md §Rule Configuration.
 */

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { RangePicker } from "@/components/range-picker";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ValidationConfForm } from "@/components/validation/validation-conf-form";
import { ValidationScoreChart } from "@/components/validation/validation-score-chart";
import { ValidationVariablesChart } from "@/components/validation/validation-variables-chart";
import { toInternal, defaultFormValues } from "@/components/validation/validation-conf-form.schema";
import { resolveRange } from "@/lib/range";
import { usePersistedRangeState, RANGE_KEYS } from "@/lib/hooks/use-range-selection";
import { ApiError } from "@/lib/api/client";
import {
  useValidationConf,
  useUpsertValidationConf,
  useDeleteValidationConf,
  useValidationResults,
} from "@/lib/api/validation";
import { useMe } from "@/lib/auth/use-me";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { ValidationConfResponse } from "@/types/validation";

const CONF_FORM_ID = "validation-conf-form";

// ── Conf read-only view ────────────────────────────────────────────────────────

function ConfReadOnly({ conf }: { conf: ValidationConfResponse }) {
  return (
    <div className="space-y-4">
      <div>
        <dt className="text-xs font-medium text-muted-foreground">description</dt>
        <dd className="mt-1 text-sm">{conf.description}</dd>
      </div>
      <div>
        <dt className="text-xs font-medium text-muted-foreground">
          variables ({conf.variables.length})
        </dt>
        <dd className="mt-2 overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs text-muted-foreground">
                <th className="w-[35%] px-3 py-1.5 text-left font-medium">name</th>
                <th className="px-3 py-1.5 text-left font-medium">description</th>
              </tr>
            </thead>
            <tbody>
              {conf.variables.map((v) => (
                <tr key={v.name} className="border-b last:border-0">
                  <td className="px-3 py-1.5 align-top font-mono text-xs">{v.name}</td>
                  <td className="px-3 py-1.5 align-top text-muted-foreground">
                    {v.description || <span className="text-muted-foreground/50">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </dd>
      </div>
    </div>
  );
}

interface ValidationDataPanelProps {
  datasetUrn: string;
}

export function ValidationDataPanel({ datasetUrn }: ValidationDataPanelProps) {
  const { canWrite } = useMe();
  const tz = useDisplayTz();

  const [isEditing, setIsEditing] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  const { selection: resultSel, setSelection: setResultSel } = usePersistedRangeState(
    RANGE_KEYS.validationResults,
  );
  const resultRange = useMemo(() => resolveRange(resultSel, "date", tz), [resultSel, tz]);

  // ── Queries ──────────────────────────────────────────────────────────────────
  const { data: conf, isLoading: confLoading, error: confError } = useValidationConf(datasetUrn);

  const { data: resultsData } = useValidationResults(datasetUrn, {
    from: resultRange.from,
    until: resultRange.to,
    limit: 1000,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────────
  const upsert = useUpsertValidationConf(datasetUrn);
  const deleteConf = useDeleteValidationConf(datasetUrn);

  // ── Handlers ─────────────────────────────────────────────────────────────────
  const handleSave = (body: Record<string, unknown>) => {
    upsert.mutate(body, { onSuccess: () => setIsEditing(false) });
  };

  const handleDelete = () => {
    // After delete the conf query invalidates → 404 → the empty-state renders
    // automatically; the useEffect below resets any lingering edit state.
    deleteConf.mutate(undefined);
  };

  // ── Error messages ────────────────────────────────────────────────────────────
  const saveError =
    upsert.error instanceof ApiError
      ? `${upsert.error.error_code}: ${upsert.error.message}`
      : upsert.error?.message;

  // ── State flags ────────────────────────────────────────────────────────────────
  // A slot is either present (200 conf) or absent (404 CONFIG_NOT_FOUND); an
  // absent slot shows the Create empty-state.
  const is404 = confError instanceof ApiError && confError.status === 404;
  const isAbsent = is404;
  const confExists = !!conf && !is404;

  // A lingering `isEditing=true` from before a delete must not resurface a form
  // once the conf is gone.
  useEffect(() => {
    if (is404) setIsEditing(false);
  }, [is404]);

  // ── Loading / error ─────────────────────────────────────────────────────────
  if (confLoading) {
    return <Skeleton className="h-48 w-full" />;
  }

  if (confError && !is404) {
    return <ErrorState message={`Failed to load validation config: ${confError.message}`} />;
  }

  // The timeseries render only in the has-conf read-only view; while editing
  // (create or edit) the panel shows the Config section alone.
  const hasTimeseries = confExists && !isEditing;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">
      {/* Conf — Config heading + actions, then read-only/edit/create */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Config</h3>
          {canWrite && (
            <div className="flex flex-wrap items-center gap-2">
              {confExists && !isEditing && (
                <>
                  <Button
                    key="conf-edit"
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setIsEditing(true)}
                  >
                    Edit
                  </Button>
                  <Button
                    key="conf-delete"
                    type="button"
                    variant="destructive"
                    size="sm"
                    onClick={() => setShowDeleteDialog(true)}
                  >
                    Delete
                  </Button>
                </>
              )}
              {isEditing && (
                <>
                  <Button
                    key="conf-cancel"
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setIsEditing(false)}
                    disabled={upsert.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    key="conf-save"
                    type="submit"
                    form={CONF_FORM_ID}
                    size="sm"
                    disabled={upsert.isPending}
                  >
                    {upsert.isPending ? "Saving..." : "Save"}
                  </Button>
                </>
              )}
              {isAbsent && !isEditing && (
                <Button
                  key="conf-create"
                  type="button"
                  size="sm"
                  onClick={() => setIsEditing(true)}
                >
                  Create
                </Button>
              )}
            </div>
          )}
        </div>

        {/* No config yet, not editing — empty-state line only */}
        {isAbsent && !isEditing && (
          <p className="text-sm text-muted-foreground">No config yet.</p>
        )}

        {/* Editing (create or edit) — the Config form */}
        {isEditing && (
          <ValidationConfForm
            formId={CONF_FORM_ID}
            defaultValues={confExists ? toInternal(conf) : defaultFormValues()}
            onSubmit={handleSave}
            serverError={saveError}
          />
        )}

        {/* Config exists, not editing — read-only view */}
        {confExists && !isEditing && <ConfReadOnly conf={conf} />}
      </div>

      {/* Historical timeseries */}
      {hasTimeseries && (
        <div className="space-y-6">
          <div>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-medium">Quality Score (attr/validation/result)</h3>
              <RangePicker value={resultSel} onChange={setResultSel} tz={tz} granularity="date" />
            </div>
            <ValidationScoreChart results={resultsData?.results ?? []} height={200} />
          </div>

          <div>
            <h3 className="mb-3 text-sm font-medium">Variables (attr/validation/result)</h3>
            <ValidationVariablesChart
              results={resultsData?.results ?? []}
              variables={confExists ? conf.variables : undefined}
              height={160}
            />
          </div>
        </div>
      )}

      {/* Delete confirm dialog */}
      {canWrite && confExists && (
        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title="Delete validation config"
          description={`Delete the validation config for "${datasetUrn}". This also removes its result history and validation events, and cannot be undone.`}
          confirmLabel="Delete"
          onConfirm={handleDelete}
          loading={deleteConf.isPending}
        />
      )}
    </div>
  );
}
