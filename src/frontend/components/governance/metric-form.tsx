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
 *   title?         — optional panel heading rendered left of the top action bar
 */

import { useCallback, useEffect } from "react";
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
import {
  METRIC_EMITTED_KEYS,
  METRIC_TYPES_WITH_TIME_WINDOW,
} from "@/types/governance";
import type {
  CreateMetricFormValues,
  DatasetFilter,
  MetricFormValues,
  MetricType,
  ScheduleTier,
} from "@/types/governance";
import {
  baseSchema,
  createSchema,
  fromInternal,
  toInternal,
  pruneMetricKeys,
} from "./metric-form.schema";
import type { InternalFormValues } from "./metric-form.schema";

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
  title?: string;
}

export function MetricForm({
  defaultValues,
  isCreate,
  onSubmit,
  onCancel,
  isPending,
  serverError,
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
  const selectedMetrics = watch("metrics");
  const datasetFilter = watch("dataset_filter") as DatasetFilter;

  const emittedKeys = METRIC_EMITTED_KEYS[metricType] ?? [];
  const needsWindow = METRIC_TYPES_WITH_TIME_WINDOW.includes(metricType);

  const handleFilterChange = useCallback(
    (v: DatasetFilter) => {
      setValue("dataset_filter", v, { shouldDirty: true });
    },
    [setValue],
  );

  const handleToggleMetricKey = useCallback(
    (key: string, checked: boolean) => {
      const current: string[] = selectedMetrics ?? [];
      const next = checked ? [...current, key] : current.filter((k: string) => k !== key);
      setValue("metrics", next, { shouldDirty: true });
    },
    [selectedMetrics, setValue],
  );

  // F3: on type change, prune stale metric keys via pruneMetricKeys and reset time_window_sec.
  const handleMetricTypeChange = useCallback(
    (newType: MetricType) => {
      setValue("metric_type", newType, { shouldDirty: true });

      const pruned = pruneMetricKeys(newType, selectedMetrics ?? []);
      setValue("metrics", pruned, { shouldDirty: true });

      const newNeedsWindow = (METRIC_TYPES_WITH_TIME_WINDOW as string[]).includes(newType);
      if (!newNeedsWindow) {
        setValue("time_window_sec", undefined, { shouldDirty: true });
      }
    },
    [selectedMetrics, setValue],
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

        {/* metrics (checkbox list) */}
        <Field
          label="metrics"
          htmlFor="metrics"
          error={(errors.metrics as { message?: string } | undefined)?.message}
          required
          hint="Which value keys to persist in results"
          className="sm:col-span-2"
        >
          <div className="flex flex-wrap gap-3">
            {emittedKeys.map((key) => (
              <label key={key} className="flex cursor-pointer items-center gap-2 text-sm">
                <Checkbox
                  id={`metric-key-${key}`}
                  checked={selectedMetrics?.includes(key) ?? false}
                  onCheckedChange={(checked) => handleToggleMetricKey(key, !!checked)}
                />
                {key}
              </label>
            ))}
          </div>
        </Field>

        {/* metric_conf (time_window_sec for freshness / validation-score) */}
        {needsWindow && (
          <Field
            label="metric_conf.time_window_sec"
            htmlFor="time-window-sec"
            error={errors.time_window_sec?.message}
            required
            hint="Fallback freshness window in seconds (positive integer)"
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
          <DatasetFilterEditor value={datasetFilter ?? {}} onChange={handleFilterChange} />
        </div>
      </FormGrid>

      {serverError && <ErrorText message={serverError} />}
    </form>
  );
}
