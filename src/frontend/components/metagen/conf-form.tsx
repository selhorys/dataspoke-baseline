"use client";

/**
 * MetagenConfForm — edit the singleton metagen global conf.
 * Fields: is_enabled, schedule_tier, dataset_filter, result_limit, overwrite_pending.
 */

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DatasetFilterEditor } from "@/components/dataset-filter-editor";
import type { MetagenGlobalConf, MetagenGlobalConfPutBody } from "@/types/metagen";
import type { DatasetFilter } from "@/types/governance";

const confSchema = z.object({
  is_enabled: z.boolean(),
  schedule_tier: z.enum(["hourly", "daily", "weekly"]).nullable(),
  result_limit: z.number().int().min(1).max(20),
  overwrite_pending: z.boolean(),
});

type ConfFormValues = z.infer<typeof confSchema>;

interface MetagenConfFormProps {
  initialValues: MetagenGlobalConf | null;
  datasetFilter: DatasetFilter;
  onDatasetFilterChange: (v: DatasetFilter) => void;
  onSubmit: (body: MetagenGlobalConfPutBody) => void;
  isSubmitting: boolean;
  disabled?: boolean;
}

export function MetagenConfForm({
  initialValues,
  datasetFilter,
  onDatasetFilterChange,
  onSubmit,
  isSubmitting,
  disabled = false,
}: MetagenConfFormProps) {
  const {
    register,
    handleSubmit,
    setValue,
    watch,
    reset,
    formState: { errors },
  } = useForm<ConfFormValues>({
    resolver: zodResolver(confSchema),
    defaultValues: {
      is_enabled: initialValues?.is_enabled ?? false,
      schedule_tier: initialValues?.schedule_tier ?? null,
      result_limit: initialValues?.result_limit ?? 3,
      overwrite_pending: initialValues?.overwrite_pending ?? true,
    },
  });

  useEffect(() => {
    if (initialValues) {
      reset({
        is_enabled: initialValues.is_enabled,
        schedule_tier: initialValues.schedule_tier ?? null,
        result_limit: initialValues.result_limit,
        overwrite_pending: initialValues.overwrite_pending,
      });
    }
  }, [initialValues, reset]);

  const isEnabled = watch("is_enabled");
  const scheduleTier = watch("schedule_tier");
  const overwritePending = watch("overwrite_pending");

  function handleFormSubmit(values: ConfFormValues) {
    onSubmit({
      is_enabled: values.is_enabled,
      schedule_tier: values.schedule_tier ?? null,
      dataset_filter: datasetFilter as Record<string, unknown>,
      result_limit: values.result_limit,
      overwrite_pending: values.overwrite_pending,
    });
  }

  return (
    <form onSubmit={handleSubmit(handleFormSubmit)} className="space-y-6">
      <Field label="is_enabled" htmlFor="metagen-conf-is-enabled">
        <div className="flex items-center gap-2">
          <Checkbox
            id="metagen-conf-is-enabled"
            checked={isEnabled}
            onCheckedChange={(v) => setValue("is_enabled", !!v)}
            disabled={disabled}
          />
          <span className="text-sm text-muted-foreground">
            Enable periodic inference DAG
          </span>
        </div>
      </Field>

      <Field
        label="schedule_tier"
        htmlFor="metagen-conf-schedule-tier"
        hint="Periodic re-inference cadence"
      >
        <Select
          value={scheduleTier || "none"}
          onValueChange={(v) =>
            setValue(
              "schedule_tier",
              v === "none" ? null : (v as "hourly" | "daily" | "weekly"),
            )
          }
          disabled={disabled}
        >
          <SelectTrigger id="metagen-conf-schedule-tier">
            <SelectValue placeholder="None (manual only)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">None (manual only)</SelectItem>
            <SelectItem value="hourly">hourly</SelectItem>
            <SelectItem value="daily">daily</SelectItem>
            <SelectItem value="weekly">weekly</SelectItem>
          </SelectContent>
        </Select>
      </Field>

      <DatasetFilterEditor
        value={datasetFilter}
        onChange={onDatasetFilterChange}
        disabled={disabled}
      />

      <Field
        label="result_limit"
        htmlFor="metagen-conf-result-limit"
        hint="Maximum number of candidates generated per item (1–20)"
        error={errors.result_limit?.message}
      >
        <Input
          id="metagen-conf-result-limit"
          type="number"
          min={1}
          max={20}
          disabled={disabled}
          {...register("result_limit", { valueAsNumber: true })}
        />
      </Field>

      <Field label="overwrite_pending" htmlFor="metagen-conf-overwrite-pending">
        <div className="flex items-center gap-2">
          <Checkbox
            id="metagen-conf-overwrite-pending"
            checked={overwritePending}
            onCheckedChange={(v) => setValue("overwrite_pending", !!v)}
            disabled={disabled}
          />
          <span className="text-sm text-muted-foreground">
            Overwrite pending (llm_approved) candidates on the next run
          </span>
        </div>
      </Field>

      {!disabled && (
        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Saving…" : "Save configuration"}
        </Button>
      )}
    </form>
  );
}
