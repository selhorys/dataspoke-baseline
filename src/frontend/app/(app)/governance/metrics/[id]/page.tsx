"use client";

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Play, Power, PowerOff, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { RangePicker } from "@/components/range-picker";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
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
import { MetricEventTable } from "@/components/governance/metric-event-table";
import { FieldPanel } from "@/components/forms/field-panel";
import { FormGrid } from "@/components/ui/form-grid";
import { DatasetFilterView } from "@/components/dataset-filter-view";
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import { ApiError } from "@/lib/api/client";
import {
  useMetricConf,
  useMetricResults,
  useMetricEvents,
  useReplaceMetricConf,
  useUpdateMetricConf,
  useDeleteMetric,
  useRunMetric,
} from "@/lib/api/governance";
import { useMe } from "@/lib/auth/use-me";
import { QueryErrorState } from "@/components/query-error-state";
import { DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricFormValues } from "@/types/governance";

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
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);
  // Range selections are persisted per surface; resolving via useMemo keeps the
  // derived bounds (and thus query keys) stable until the selection changes.
  // Results chart and event log keep independent ranges.
  const tz = useDisplayTz();
  const { selection: resultSel, setSelection: setResultSel } =
    usePersistedRangeState(RANGE_KEYS.governanceMetricResults);
  const { selection: eventSel, setSelection: setEventSel } =
    usePersistedRangeState(RANGE_KEYS.governanceMetricEvents);
  const resultRange = useMemo(
    () => resolveRange(resultSel, "date", tz),
    [resultSel, tz],
  );
  const eventRange = useMemo(
    () => resolveRange(eventSel, "datetime", tz),
    [eventSel, tz],
  );

  // ── Queries ────────────────────────────────────────────────────────────────
  const { data: conf, isLoading: confLoading, error: confError } = useMetricConf(metricId);
  // This endpoint uses `to` (not `until`) for the upper bound.
  const { data: resultsData } = useMetricResults(metricId, {
    from: resultRange.from,
    to: resultRange.to,
    limit: 100,
  });
  const { data: eventsData } = useMetricEvents(metricId, {
    from: eventRange.from,
    to: eventRange.to,
    offset: eventOffset,
    limit: eventLimit,
    sort: "occurred_at_desc",
  });

  // Reset event pagination when the time filter changes.
  useEffect(() => {
    setEventOffset(0);
  }, [eventSel]);

  // ── Mutations ──────────────────────────────────────────────────────────────
  const replace = useReplaceMetricConf();
  const updateConf = useUpdateMetricConf();
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
        <QueryErrorState error={confError} context="Failed to load metric" />
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
            <Button
              variant="outline"
              size="sm"
              data-testid="metric-toggle-enabled"
              disabled={updateConf.isPending}
              onClick={() =>
                updateConf.mutate(
                  { metricId, patch: { is_enabled: !conf.is_enabled } },
                  {
                    onError: (err) => {
                      const message =
                        err instanceof ApiError
                          ? `${err.error_code}: ${err.message}`
                          : err.message;
                      toast({ title: message, variant: "destructive" });
                    },
                  },
                )
              }
            >
              {conf.is_enabled ? (
                <PowerOff className="mr-1 h-3.5 w-3.5" />
              ) : (
                <Power className="mr-1 h-3.5 w-3.5" />
              )}
              {updateConf.isPending
                ? "Saving..."
                : conf.is_enabled
                  ? "Disable"
                  : "Enable"}
            </Button>
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
        {/* While editing, the form's `title` prop renders the attr/conf heading
            (and the Cancel/Save buttons on the same row), so suppress the
            page's own heading to avoid duplication. */}
        {!isEditing && <h2 className="mb-3 text-sm font-medium">Config</h2>}

        {!isEditing ? (
          <FormGrid>
            <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:col-span-2 sm:grid-cols-4">
              <FieldPanel label="mode">{conf.mode}</FieldPanel>

              <FieldPanel label="metric_type">
                <Badge variant="outline">{conf.metric_type}</Badge>
              </FieldPanel>

              <FieldPanel label="schedule_tier">
                <ScheduleTierLink
                  tier={conf.schedule_tier ?? "on-demand"}
                  dagId={scheduleDagId("metrics", conf.schedule_tier)}
                />
              </FieldPanel>

              <FieldPanel label="is_enabled">
                <Badge variant={conf.is_enabled ? "default" : "secondary"}>
                  {conf.is_enabled ? "Enabled" : "Disabled"}
                </Badge>
              </FieldPanel>
            </div>

            <FieldPanel label="metrics" className="sm:col-span-2">
              <span className="font-mono text-xs">{conf.metrics.join(", ")}</span>
            </FieldPanel>

            <FieldPanel label="description" className="sm:col-span-2">
              {conf.description}
            </FieldPanel>

            {conf.metric_conf && Object.keys(conf.metric_conf).length > 0 && (
              <FieldPanel label="metric_conf" className="sm:col-span-2">
                <span className="font-mono text-xs">
                  {JSON.stringify(conf.metric_conf)}
                </span>
              </FieldPanel>
            )}

            <div className="sm:col-span-2">
              <DatasetFilterView value={conf.dataset_filter} />
            </div>
          </FormGrid>
        ) : (
          <MetricForm
            title="Config"
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
          <h2 className="text-sm font-medium">Result</h2>
          <RangePicker
            value={resultSel}
            onChange={setResultSel}
            tz={tz}
            granularity="date"
          />
        </div>
        <MetricTimeseriesChart results={resultsData?.results ?? []} height={200} />
      </section>

      {/* event log */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Event</h2>
        <MetricEventTable
          events={eventsData?.events ?? []}
          range={eventSel}
          onRangeChange={setEventSel}
          tz={tz}
          page={{
            offset: eventOffset,
            limit: eventLimit,
            totalCount: eventsData?.total_count ?? 0,
          }}
          onOffset={setEventOffset}
          onLimit={setEventLimit}
        />
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
