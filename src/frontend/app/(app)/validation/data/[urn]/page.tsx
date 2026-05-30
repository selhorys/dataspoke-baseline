"use client";

import { use, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";
import { ErrorState } from "@/components/ui/error-state";
import type { ValidationConfResponse } from "@/types/validation";

// ── Date helpers ───────────────────────────────────────────────────────────────

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

// ── Range selector ─────────────────────────────────────────────────────────────

const RANGE_OPTIONS = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

type RangeLabel = (typeof RANGE_OPTIONS)[number]["label"];

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
        <dd className="mt-2 flex flex-wrap gap-1.5">
          {conf.variables.map((v) => (
            <Badge key={v} variant="outline" className="font-mono text-xs">
              {v}
            </Badge>
          ))}
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
  const [rangeLabel, setRangeLabel] = useState<RangeLabel>("30d");

  const rangeDays = RANGE_OPTIONS.find((r) => r.label === rangeLabel)?.days ?? 30;
  // from is recomputed each render; no upper bound — open-ended (backend defaults to now).
  const from = daysAgoIso(rangeDays);

  // ── Queries ──────────────────────────────────────────────────────────────────
  const {
    data: conf,
    isLoading: confLoading,
    error: confError,
  } = useValidationConf(datasetUrn);

  const { data: resultsData } = useValidationResults(datasetUrn, {
    from,
    limit: 1000,
  });

  const { data: eventsData } = useValidationEvents(datasetUrn, 5);

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

        {canWrite && conf && !isEditing && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setIsEditing(true)}>
              Edit
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setShowDeleteDialog(true)}
            >
              Delete
            </Button>
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
                  defaultValues={defaultFormValues()}
                  onSubmit={handleSave}
                  isPending={upsert.isPending}
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
        {conf && !isEditing && <ConfReadOnly conf={conf} />}
        {conf && isEditing && (
          <ValidationConfForm
            defaultValues={toInternal(conf)}
            onSubmit={handleSave}
            onCancel={() => setIsEditing(false)}
            isPending={upsert.isPending}
            serverError={saveError}
          />
        )}
      </section>

      {/* Historical timeseries — only meaningful when a config exists or results exist */}
      {(conf || (resultsData && resultsData.results.length > 0)) && (
        <>
          {/* Score chart */}
          <section className="rounded-lg border p-5">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-medium">Quality Score (attr/validation/result)</h2>
              <Select
                value={rangeLabel}
                onValueChange={(v) => setRangeLabel(v as RangeLabel)}
              >
                <SelectTrigger className="w-[100px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {RANGE_OPTIONS.map((r) => (
                    <SelectItem key={r.label} value={r.label}>
                      {r.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
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
              allVariables={conf?.variables}
              height={220}
            />
          </section>
        </>
      )}

      {/* event/validation (latest 5) */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">event/validation (latest 5)</h2>
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
                  {formatDateTime(e.occurred_at)}
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
      {canWrite && conf && (
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
