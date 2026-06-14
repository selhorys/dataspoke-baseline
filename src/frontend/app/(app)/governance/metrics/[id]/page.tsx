"use client";

import { use, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Play, Trash2 } from "lucide-react";
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
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetricForm } from "@/components/governance/metric-form";
import { MetricTimeseriesChart } from "@/components/governance/metric-timeseries-chart";
import { ApiError } from "@/lib/api/client";
import {
  useMetricConf,
  useMetricResults,
  useMetricEvents,
  useReplaceMetricConf,
  useDeleteMetric,
  useRunMetric,
} from "@/lib/api/governance";
import { useMe } from "@/lib/auth/use-me";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { ErrorState } from "@/components/ui/error-state";
import type { MetricFormValues } from "@/types/governance";

// ── Date helpers ───────────────────────────────────────────────────────────────

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// ── Range selector ─────────────────────────────────────────────────────────────

const RANGE_OPTIONS = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

type RangeLabel = (typeof RANGE_OPTIONS)[number]["label"];

// ── Page component ─────────────────────────────────────────────────────────────

export default function MetricDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: metricId } = use(params);
  const router = useRouter();
  const { canWrite } = useMe();

  const [isEditing, setIsEditing] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showRunDialog, setShowRunDialog] = useState(false);
  const [dryRun, setDryRun] = useState(false);
  const [rangeLabel, setRangeLabel] = useState<RangeLabel>("30d");

  const rangeDays = RANGE_OPTIONS.find((r) => r.label === rangeLabel)?.days ?? 30;
  // `from` feeds the results query key; memoize on rangeDays so the key only
  // changes when the selected range changes — not on every render. No `to` —
  // open-ended (backend defaults to now).
  const from = useMemo(() => daysAgoIso(rangeDays), [rangeDays]);

  // ── Queries ────────────────────────────────────────────────────────────────
  const { data: conf, isLoading: confLoading, error: confError } = useMetricConf(metricId);
  const { data: resultsData } = useMetricResults(metricId, { from, limit: 100 });
  const { data: eventsData } = useMetricEvents(metricId);

  // ── Mutations ──────────────────────────────────────────────────────────────
  const replace = useReplaceMetricConf();
  const deleteMetric = useDeleteMetric();
  const runMetric = useRunMetric();

  // ── Event handler helpers ──────────────────────────────────────────────────

  const handleSave = (values: MetricFormValues) => {
    replace.mutate(
      { metricId, values },
      {
        onSuccess: () => setIsEditing(false),
      },
    );
  };

  const handleDelete = () => {
    deleteMetric.mutate(
      { metricId },
      {
        onSuccess: () => router.push("/governance/metrics"),
      },
    );
  };

  const handleRun = () => {
    runMetric.mutate(
      { metricId, dry_run: dryRun },
      {
        onSuccess: () => {
          setShowRunDialog(false);
          toast({ title: dryRun ? "Dry run complete" : "Run complete" });
        },
      },
    );
  };

  // ── Error messages ──────────────────────────────────────────────────────────

  const saveError =
    replace.error instanceof ApiError
      ? `${replace.error.error_code}: ${replace.error.message}`
      : replace.error?.message;

  const runError =
    runMetric.error instanceof ApiError
      ? `${runMetric.error.error_code}: ${runMetric.error.message}`
      : runMetric.error?.message;

  // ── Loading / error states ──────────────────────────────────────────────────

  if (confLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (confError) {
    return (
      <div className="space-y-2">
        <ErrorState message={`Failed to load metric: ${confError.message}`} />
        <Button variant="outline" size="sm" asChild>
          <Link href="/governance/metrics">Back to list</Link>
        </Button>
      </div>
    );
  }

  if (!conf) return null;

  // ── Default form values from conf ─────────────────────────────────────────
  const formDefaults: MetricFormValues = {
    mode: conf.mode as MetricFormValues["mode"],
    metric_type: conf.metric_type as MetricFormValues["metric_type"],
    title: conf.title,
    description: conf.description,
    metrics: conf.metrics,
    metric_conf: conf.metric_conf,
    schedule_tier: conf.schedule_tier,
    is_enabled: conf.is_enabled,
    dataset_filter: conf.dataset_filter,
  };

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Link
          href="/governance/metrics"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to metrics"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">{conf.title}</h1>
        <span className="font-mono text-sm text-muted-foreground">{conf.id}</span>

        {canWrite && !isEditing && (
          <div className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setIsEditing(true)}>
              Edit
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setDryRun(false);
                setShowRunDialog(true);
              }}
            >
              <Play className="mr-1 h-3.5 w-3.5" />
              Run
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setShowDeleteDialog(true)}
            >
              <Trash2 className="mr-1 h-3.5 w-3.5" />
              Delete
            </Button>
          </div>
        )}
      </div>

      {/* attr/conf — read-only summary or edit form */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">attr/conf</h2>

        {!isEditing ? (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-muted-foreground">mode</dt>
              <dd>{conf.mode}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">metric_type</dt>
              <dd>
                <Badge variant="outline">{conf.metric_type}</Badge>
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">schedule_tier</dt>
              <dd>{conf.schedule_tier ?? "on-demand"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">is_enabled</dt>
              <dd>
                <Badge variant={conf.is_enabled ? "default" : "secondary"}>
                  {conf.is_enabled ? "Enabled" : "Disabled"}
                </Badge>
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">metrics</dt>
              <dd className="font-mono text-xs">{conf.metrics.join(", ")}</dd>
            </div>
            {conf.metric_conf && Object.keys(conf.metric_conf).length > 0 && (
              <div>
                <dt className="text-muted-foreground">metric_conf</dt>
                <dd className="font-mono text-xs">
                  {JSON.stringify(conf.metric_conf)}
                </dd>
              </div>
            )}
            {conf.dataset_filter && Object.keys(conf.dataset_filter).length > 0 && (
              <div className="col-span-full">
                <dt className="text-muted-foreground">dataset_filter</dt>
                <dd className="font-mono text-xs">
                  {JSON.stringify(conf.dataset_filter)}
                </dd>
              </div>
            )}
          </dl>
        ) : (
          <MetricForm
            defaultValues={formDefaults}
            isCreate={false}
            onSubmit={(values) => handleSave(values as MetricFormValues)}
            onCancel={() => setIsEditing(false)}
            isPending={replace.isPending}
            serverError={saveError}
          />
        )}
      </section>

      {/* attr/result — timeseries chart */}
      <section className="rounded-lg border p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium">attr/result</h2>
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
        <MetricTimeseriesChart results={resultsData?.results ?? []} height={200} />
      </section>

      {/* event log */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">event</h2>
        {eventsData?.events.length === 0 && (
          <p className="text-sm text-muted-foreground">No events yet.</p>
        )}
        <ul className="space-y-2">
          {eventsData?.events.map((e) => (
            <li key={e.id} className="flex items-start gap-3 text-sm">
              <span className="text-muted-foreground">{formatDateTime(e.occurred_at)}</span>
              <Badge
                variant={eventStatusVariant(e.status)}
                className="text-xs"
              >
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
      </section>

      {/* Run dialog */}
      {canWrite && (
        <Dialog open={showRunDialog} onOpenChange={setShowRunDialog}>
          <DialogContent className="sm:max-w-[425px]">
            <DialogHeader>
              <DialogTitle>Run metric</DialogTitle>
              <DialogDescription>
                {`Trigger a measurement run for "${conf.title}".`}
              </DialogDescription>
            </DialogHeader>

            <div className="flex items-center gap-2 py-2">
              <Checkbox
                id="run-dry-run"
                checked={dryRun}
                onCheckedChange={(v) => setDryRun(!!v)}
                disabled={runMetric.isPending}
              />
              <label htmlFor="run-dry-run" className="cursor-pointer text-sm">
                Dry run — evaluate without persisting a result
              </label>
            </div>

            {runError && <p className="text-sm text-destructive">{runError}</p>}

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setShowRunDialog(false)}
                disabled={runMetric.isPending}
              >
                Cancel
              </Button>
              <Button onClick={handleRun} disabled={runMetric.isPending}>
                {runMetric.isPending ? "Running..." : dryRun ? "Dry run" : "Run"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Delete confirm */}
      {canWrite && (
        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title="Delete metric"
          description={`Permanently delete "${conf.title}" and all its results. This cannot be undone.`}
          confirmLabel="Delete"
          onConfirm={handleDelete}
          loading={deleteMetric.isPending}
        />
      )}
    </div>
  );
}
