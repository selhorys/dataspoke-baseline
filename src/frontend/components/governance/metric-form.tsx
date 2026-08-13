"use client";

/**
 * MetricForm — shared form for creating and editing a metric definition.
 *
 * When `isCreate=true`, renders a leading metric_id field (create-only).
 * When `isCreate=false`, metric_id comes from the route param and is shown
 * read-only above the form.
 *
 * Props:
 *   defaultValues  — initial form values
 *   isCreate       — true on /governance/metrics/new
 *   onSubmit       — called with validated form data
 *   isPending      — shows loading state on the Save button
 *   serverError?   — top-level error from the mutation
 *   filterError?   — 422 INVALID_DATASET_FILTER, rendered inline in the editor
 *   title?         — optional panel heading rendered left of the top action bar
 *
 * The `metrics` control is one row per emitted key of the selected metric_type:
 * a checkbox, a color control (native swatch paired with a #RRGGBB text input),
 * and a display-order number. Only checked rows are submitted, as
 * `{name, color, idx}` (spec/feature/FRONTEND_GOVERNANCE.md §Metrics).
 */

import { useCallback, useEffect, useMemo } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Field } from "@/components/forms/field";
import { FormGrid } from "@/components/ui/form-grid";
import { ErrorText } from "@/components/forms/error-text";
import { DatasetFilterEditor } from "@/components/dataset-filter-editor";
import { METRIC_TYPES_WITH_TIME_WINDOW } from "@/types/governance";
import type {
  CreateMetricFormValues,
  MetricFormValues,
  MetricType,
  ScheduleTier,
} from "@/types/governance";
import type { DatasetFilterErrorInfo } from "@/lib/dataset-filter-error";
import {
  SERIES_COLOR_PATTERN,
  baseSchema,
  createSchema,
  fromInternal,
  seriesRowsForType,
  toInternal,
} from "./metric-form.schema";
import type { InternalFormValues, MetricSeriesRow } from "./metric-form.schema";

// ── Internal form value shape ─────────────────────────────────────────────────

// Both schemas produce the same runtime shape; use the create schema's inferred type.
type InternalCreate = InternalFormValues;

// ── Component ─────────────────────────────────────────────────────────────────

interface MetricFormProps {
  defaultValues: MetricFormValues;
  isCreate: boolean;
  onSubmit: (values: MetricFormValues | CreateMetricFormValues) => void;
  onCancel?: () => void;
  isPending: boolean;
  serverError?: string;
  filterError?: DatasetFilterErrorInfo;
  title?: string;
}

export function MetricForm({
  defaultValues,
  isCreate,
  onSubmit,
  onCancel,
  isPending,
  serverError,
  filterError,
  title,
}: MetricFormProps) {
  const schema = isCreate ? createSchema : baseSchema;

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
    reset,
  } = useForm<InternalCreate>({
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    resolver: zodResolver(schema as unknown as z.ZodType<InternalCreate>),
    defaultValues: {
      ...toInternal(defaultValues),
      metric_id: "",
    },
  });

  // Reset when defaultValues change (e.g. after data loads in edit mode).
  useEffect(() => {
    reset({ ...toInternal(defaultValues), metric_id: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultValues.metric_type, defaultValues.title]);

  const mode = watch("mode");
  const metricType = watch("metric_type") as MetricType;
  const watchedMetrics = watch("metrics");
  // Memoised so the row-patch callbacks below do not change identity every render.
  const seriesRows = useMemo(
    () => (watchedMetrics ?? []) as MetricSeriesRow[],
    [watchedMetrics],
  );
  const datasetFilter = watch("dataset_filter") as string;

  // `dataset_filter` is driven by setValue (never registered), so its schema
  // error has no other render site: without this the length cap would make
  // handleSubmit a silent no-op. The server 422 wins when both are present.
  const clientFilterError: DatasetFilterErrorInfo | undefined = errors.dataset_filter
    ?.message
    ? { message: errors.dataset_filter.message }
    : undefined;

  const needsWindow = METRIC_TYPES_WITH_TIME_WINDOW.includes(metricType);

  // Per-row errors from the series refinement (color / idx), keyed by row index.
  const seriesErrors = errors.metrics as
    | ({ message?: string } & Array<
        { color?: { message?: string }; idx?: { message?: string } } | undefined
      >)
    | undefined;

  const handleFilterChange = useCallback(
    (v: string) => {
      setValue("dataset_filter", v, { shouldDirty: true });
    },
    [setValue],
  );

  const patchSeriesRow = useCallback(
    (index: number, patch: Partial<MetricSeriesRow>) => {
      const next = seriesRows.map((row, i) => (i === index ? { ...row, ...patch } : row));
      setValue("metrics", next, { shouldDirty: true, shouldValidate: false });
    },
    [seriesRows, setValue],
  );

  // F3: on type change, reseed the series rows to the new type's emitted keys
  // (surviving keys keep their color and order) and reset time_window_sec.
  const handleMetricTypeChange = useCallback(
    (newType: MetricType) => {
      setValue("metric_type", newType, { shouldDirty: true });
      setValue("metrics", seriesRowsForType(newType, seriesRows), { shouldDirty: true });

      const newNeedsWindow = (METRIC_TYPES_WITH_TIME_WINDOW as string[]).includes(newType);
      if (!newNeedsWindow) {
        setValue("time_window_sec", undefined, { shouldDirty: true });
      }
    },
    [seriesRows, setValue],
  );

  const isPassive = mode === "passive";

  const onValid = (data: InternalCreate) => {
    const base = fromInternal(data);
    if (isCreate) {
      onSubmit({ ...base, metric_id: data.metric_id } as CreateMetricFormValues);
    } else {
      onSubmit(base);
    }
  };

  return (
    <form onSubmit={handleSubmit(onValid)} className="space-y-5">
      {/* Top action bar: optional heading on the left, buttons right-aligned. */}
      <div className="flex items-center justify-between gap-2">
        {title ? (
          <h2 className="text-sm font-medium">{title}</h2>
        ) : (
          <span />
        )}
        <div className="flex items-center gap-2">
          {isPassive && (
            <p className="text-xs text-muted-foreground">
              Passive mode not yet supported
            </p>
          )}
          {onCancel && (
            <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
              Cancel
            </Button>
          )}
          <Button type="submit" disabled={isPending || isPassive}>
            {isPending ? "Saving..." : "Save"}
          </Button>
        </div>
      </div>

      <FormGrid>
        {isCreate && (
          <Field
            label="metric_id"
            htmlFor="metric-id"
            error={errors.metric_id?.message as string | undefined}
            required
            hint="Kebab-case identifier, e.g. doc-health-dev (immutable after create)"
          >
            <Input
              id="metric-id"
              {...register("metric_id")}
              placeholder="doc-health-dev"
            />
          </Field>
        )}

        {/* mode */}
        <Field label="mode" htmlFor="mode-active" error={undefined} className="sm:col-span-2">
          <div className="flex gap-4">
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                {...register("mode")}
                value="active"
                id="mode-active"
                className="accent-primary"
              />
              active
            </label>
            <label className="flex cursor-not-allowed items-center gap-2 text-sm text-muted-foreground">
              <input
                type="radio"
                {...register("mode")}
                value="passive"
                id="mode-passive"
                className="accent-primary"
                disabled
              />
              passive — not yet supported
            </label>
          </div>
        </Field>

        {/* metric_type */}
        <Field
          label="metric_type"
          htmlFor="metric-type"
          error={errors.metric_type?.message}
          required
        >
          <Select
            value={metricType}
            onValueChange={(v) => handleMetricTypeChange(v as MetricType)}
          >
            <SelectTrigger id="metric-type">
              <SelectValue placeholder="Select type" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ingestion-freshness">ingestion-freshness</SelectItem>
              <SelectItem value="validation-score">validation-score</SelectItem>
              <SelectItem value="doc-health">doc-health</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        {/* title */}
        <Field label="title" htmlFor="title" error={errors.title?.message as string | undefined} required>
          <Input id="title" {...register("title")} placeholder="Doc Health (DEV)" />
        </Field>

        {/* description */}
        <Field
          label="description"
          htmlFor="description"
          error={errors.description?.message as string | undefined}
          required
          className="sm:col-span-2"
        >
          <Textarea
            id="description"
            {...register("description")}
            rows={2}
            placeholder="Daily documentation-completeness check"
          />
        </Field>

        {/* metrics — one row per emitted key: checkbox, color, display order */}
        <Field
          label="metrics"
          htmlFor="metrics"
          error={seriesErrors?.message}
          required
          hint="Which value keys to persist, the line color each is drawn in, and their display order"
          className="sm:col-span-2"
        >
          <div className="space-y-2">
            {seriesRows.map((row, index) => {
              const rowErrors = seriesErrors?.[index];
              const swatch = SERIES_COLOR_PATTERN.test(row.color) ? row.color : "#000000";
              return (
                <div key={row.name} className="flex flex-wrap items-center gap-2">
                  <label
                    className="flex w-52 cursor-pointer items-center gap-2 text-sm"
                    htmlFor={`metric-key-${row.name}`}
                  >
                    <Checkbox
                      id={`metric-key-${row.name}`}
                      checked={row.selected}
                      onCheckedChange={(checked) =>
                        patchSeriesRow(index, { selected: !!checked })
                      }
                    />
                    {row.name}
                  </label>

                  <input
                    type="color"
                    aria-label={`${row.name} color swatch`}
                    value={swatch}
                    disabled={!row.selected}
                    onChange={(e) =>
                      patchSeriesRow(index, { color: e.target.value.toUpperCase() })
                    }
                    className="h-9 w-10 cursor-pointer rounded-md border border-input bg-background p-1 disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <Input
                    aria-label={`${row.name} color`}
                    value={row.color}
                    disabled={!row.selected}
                    onChange={(e) => patchSeriesRow(index, { color: e.target.value })}
                    placeholder="#2563EB"
                    className="w-28 font-mono text-xs"
                  />

                  <Label htmlFor={`metric-idx-${row.name}`} className="text-xs text-muted-foreground">
                    idx
                  </Label>
                  <Input
                    id={`metric-idx-${row.name}`}
                    aria-label={`${row.name} display order`}
                    type="number"
                    min={1}
                    value={Number.isFinite(row.idx) ? row.idx : ""}
                    disabled={!row.selected}
                    onChange={(e) =>
                      patchSeriesRow(index, { idx: Number(e.target.value) })
                    }
                    className="w-20"
                  />

                  <ErrorText message={rowErrors?.color?.message} />
                  <ErrorText message={rowErrors?.idx?.message} />
                </div>
              );
            })}
          </div>
        </Field>

        {/* metric_conf (time_window_sec for freshness / validation-score) */}
        {needsWindow && (
          <Field
            label="metric_conf.time_window_sec"
            htmlFor="time-window-sec"
            error={errors.time_window_sec?.message}
            required
            hint="Measurement window in seconds, applied to every dataset (positive integer)"
          >
            <Input
              id="time-window-sec"
              type="number"
              min={1}
              {...register("time_window_sec")}
              placeholder="172800"
            />
          </Field>
        )}
        {!needsWindow && (
          <p className="text-sm text-muted-foreground sm:col-span-2">
            metric_conf: <span className="font-mono">{"{}"}</span> — no configuration required for{" "}
            {metricType}.
          </p>
        )}

        {/* schedule_tier */}
        <Field label="schedule_tier" htmlFor="schedule-tier" error={errors.schedule_tier?.message as string | undefined}>
          <Select
            value={watch("schedule_tier") || "none"}
            onValueChange={(v) =>
              setValue("schedule_tier", v === "none" ? null : (v as ScheduleTier), {
                shouldDirty: true,
              })
            }
          >
            <SelectTrigger id="schedule-tier">
              <SelectValue placeholder="On-demand only" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">On-demand only</SelectItem>
              <SelectItem value="hourly">hourly</SelectItem>
              <SelectItem value="daily">daily</SelectItem>
              <SelectItem value="weekly">weekly</SelectItem>
            </SelectContent>
          </Select>
        </Field>

        {/* is_enabled */}
        <div className="flex items-center gap-2 sm:col-span-2">
          <Checkbox
            id="is-enabled"
            checked={watch("is_enabled")}
            onCheckedChange={(checked) =>
              setValue("is_enabled", !!checked, { shouldDirty: true })
            }
          />
          <Label htmlFor="is-enabled" className="cursor-pointer text-sm">
            is_enabled
          </Label>
        </div>

        {/* dataset_filter */}
        <div className="sm:col-span-2">
          <DatasetFilterEditor
            value={datasetFilter ?? ""}
            onChange={handleFilterChange}
            error={filterError ?? clientFilterError}
          />
        </div>
      </FormGrid>

      {serverError && <ErrorText message={serverError} />}
    </form>
  );
}
