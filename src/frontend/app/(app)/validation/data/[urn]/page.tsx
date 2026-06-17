"use client";

import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { RangePicker } from "@/components/range-picker";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ValidationConfForm } from "@/components/validation/validation-conf-form";
import { ValidationScoreChart } from "@/components/validation/validation-score-chart";
import { ValidationVariablesChart } from "@/components/validation/validation-variables-chart";
import { ApiError } from "@/lib/api/client";
import {
  useValidationConf,
  useUpsertValidationConf,
  useDeleteValidationConf,
  useValidationResults,
  useValidationEvents,
} from "@/lib/api/validation";
import { useMe } from "@/lib/auth/use-me";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { toInternal, defaultFormValues } from "@/components/validation/validation-conf-form.schema";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";
import { ErrorState } from "@/components/ui/error-state";
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
                <th className="w-[35%] px-3 py-1.5 text-left font-medium">
                  name
                </th>
                <th className="px-3 py-1.5 text-left font-medium">
                  description
                </th>
              </tr>
            </thead>
            <tbody>
              {conf.variables.map((v) => (
                <tr key={v.name} className="border-b last:border-0">
                  <td className="px-3 py-1.5 align-top font-mono text-xs">
                    {v.name}
                  </td>
                  <td className="px-3 py-1.5 align-top text-muted-foreground">
                    {v.description || (
                      <span className="text-muted-foreground/50">—</span>
                    )}
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

// ── Page ───────────────────────────────────────────────────────────────────────

export default function ValidationDetailPage({
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
  const router = useRouter();

  const [isEditing, setIsEditing] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  // Range selections are persisted per surface; resolving via useMemo keeps the
  // derived bounds (and thus query keys) stable until the selection changes.
  // The results chart and the event log keep independent ranges.
  const tz = useDisplayTz();
  const { selection: resultSel, setSelection: setResultSel } =
    usePersistedRangeState(RANGE_KEYS.validationResults);
  const { selection: eventSel, setSelection: setEventSel } =
    usePersistedRangeState(RANGE_KEYS.validationEvents);
  const resultRange = useMemo(
    () => resolveRange(resultSel, "date", tz),
    [resultSel, tz],
  );
  const eventRange = useMemo(
    () => resolveRange(eventSel, "datetime", tz),
    [eventSel, tz],
  );

  // ── Queries ──────────────────────────────────────────────────────────────────
  const {
    data: conf,
    isLoading: confLoading,
    error: confError,
  } = useValidationConf(datasetUrn);

  // Validation result uses `until` (not `to`) for the upper bound.
  const { data: resultsData } = useValidationResults(datasetUrn, {
    from: resultRange.from,
    until: resultRange.to,
    limit: 1000,
  });

  const { data: eventsData } = useValidationEvents(datasetUrn, {
    limit: 5,
    from: eventRange.from,
    to: eventRange.to,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────────
  const upsert = useUpsertValidationConf(datasetUrn);
  const deleteConf = useDeleteValidationConf(datasetUrn);

  // ── Handlers ─────────────────────────────────────────────────────────────────

  const handleSave = (body: Record<string, unknown>) => {
    upsert.mutate(body, {
      onSuccess: () => setIsEditing(false),
    });
  };

  const handleDelete = () => {
    deleteConf.mutate(undefined, {
      onSuccess: () => {
        router.push("/validation");
      },
    });
  };

  // ── Error messages ────────────────────────────────────────────────────────────

  const saveError =
    upsert.error instanceof ApiError
      ? `${upsert.error.error_code}: ${upsert.error.message}`
      : upsert.error?.message;

  // ── 404 → empty config state ───────────────────────────────────────────────
  const is404 = confError instanceof ApiError && confError.status === 404;
  // TanStack Query retains the last successful `data` when a refetch errors, so
  // after a soft-delete `conf` can be stale while `is404` is true. This single
  // authoritative flag keeps the create-form and existing-conf branches mutually
  // exclusive.
  const confExists = !!conf && !is404;

  // A lingering `isEditing=true` from before a delete must not resurface a form
  // once the conf is gone.
  useEffect(() => {
    if (is404) setIsEditing(false);
  }, [is404]);

  // Latest score for the header.
  const latestScore = resultsData?.results?.[0]?.score ?? null;

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
        <ErrorState message={`Failed to load validation config: ${confError.message}`} />
        <Button variant="outline" size="sm" asChild>
          <Link href="/validation">Back to list</Link>
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
          href="/validation"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to validation list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="truncate font-mono text-lg font-semibold tracking-tight">
          {datasetUrn}
        </h1>

        {latestScore !== null && (
          <Badge
            variant={scoreBadgeVariant(latestScore)}
            className="text-xs"
          >
            Latest score {scoreLabel(latestScore)}
          </Badge>
        )}

        {canWrite && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
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
            {confExists && isEditing && (
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
            {is404 && (
              <Button
                key="conf-create"
                type="submit"
                form={CONF_FORM_ID}
                size="sm"
                disabled={upsert.isPending}
              >
                {upsert.isPending ? "Saving..." : "Create"}
              </Button>
            )}
          </div>
        )}
      </div>

      {/* attr/validation/conf */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">attr/validation/conf</h2>

        {/* No config yet — show create form or empty state */}
        {is404 && (
          <>
            {canWrite ? (
              <>
                <p className="mb-4 text-sm text-muted-foreground">
                  No validation config exists for this dataset. Create one below.
                </p>
                <ValidationConfForm
                  formId={CONF_FORM_ID}
                  defaultValues={defaultFormValues()}
                  onSubmit={handleSave}
                  serverError={saveError}
                />
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                No validation config for this dataset.
              </p>
            )}
          </>
        )}

        {/* Config exists — read-only or edit form */}
        {confExists && !isEditing && <ConfReadOnly conf={conf} />}
        {confExists && isEditing && (
          <ValidationConfForm
            formId={CONF_FORM_ID}
            defaultValues={toInternal(conf)}
            onSubmit={handleSave}
            serverError={saveError}
          />
        )}
      </section>

      {/* Historical timeseries — only meaningful when a config exists or results exist */}
      {(confExists || (resultsData && resultsData.results.length > 0)) && (
        <>
          {/* Score chart */}
          <section className="rounded-lg border p-5">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-medium">Quality Score (attr/validation/result)</h2>
              <RangePicker
                value={resultSel}
                onChange={setResultSel}
                tz={tz}
                granularity="date"
              />
            </div>
            <ValidationScoreChart
              results={resultsData?.results ?? []}
              height={200}
            />
          </section>

          {/* Per-variable chart */}
          <section className="rounded-lg border p-5">
            <h2 className="mb-3 text-sm font-medium">
              Variables (attr/validation/result)
            </h2>
            <ValidationVariablesChart
              results={resultsData?.results ?? []}
              variables={confExists ? conf.variables : undefined}
              height={160}
            />
          </section>
        </>
      )}

      {/* event/validation (latest 5) */}
      <section className="rounded-lg border p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium">event/validation (latest 5)</h2>
          <RangePicker
            value={eventSel}
            onChange={setEventSel}
            tz={tz}
            granularity="datetime"
          />
        </div>
        {!eventsData && (
          <p className="text-sm text-muted-foreground">Loading events...</p>
        )}
        {eventsData && eventsData.events.length === 0 && (
          <p className="text-sm text-muted-foreground">No events yet.</p>
        )}
        {eventsData && eventsData.events.length > 0 && (
          <ul className="space-y-2">
            {eventsData.events.map((e) => (
              <li key={e.id} className="flex flex-wrap items-start gap-3 text-sm">
                <span className="shrink-0 text-muted-foreground">
                  {formatDateTime(e.occurred_at, tz)}
                </span>
                <Badge variant={eventStatusVariant(e.status)} className="text-xs">
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

      {/* Delete confirm dialog */}
      {canWrite && confExists && (
        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title="Delete validation config"
          description={`Soft-delete the validation config for "${datasetUrn}". You will be returned to the validation list.`}
          confirmLabel="Delete"
          onConfirm={handleDelete}
          loading={deleteConf.isPending}
        />
      )}
    </div>
  );
}
