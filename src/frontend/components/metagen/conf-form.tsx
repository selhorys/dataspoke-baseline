"use client";

/**
 * MetagenConfForm — create / edit a metagen conf (collection model).
 * Fields: name, is_enabled, schedule_tier, dataset_filter, result_limit,
 * overwrite_pending.
 *
 * Submits a full conf body; the page wires it to POST (create) or PUT (edit).
 */

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
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
import type { MetagenConf, MetagenConfPutBody } from "@/types/metagen";
import type { DatasetFilter } from "@/types/governance";

const confSchema = z.object({
  name: z.string().min(1, "Name is required").max(200),
  is_enabled: z.boolean(),
  schedule_tier: z.enum(["hourly", "daily", "weekly"]).nullable(),
  result_limit: z.number().int().min(1).max(20),
  overwrite_pending: z.boolean(),
});

type ConfFormValues = z.infer<typeof confSchema>;

interface MetagenConfFormProps {
  initialValues: MetagenConf | null;
  datasetFilter: DatasetFilter;
  onDatasetFilterChange: (v: DatasetFilter) => void;
  onSubmit: (body: MetagenConfPutBody) => void;
  disabled?: boolean;
  /** Server error (e.g. 409 METAGEN_CONF_EXISTS) to surface against the name field. */
  serverError?: string;
  /** id wired to the top-right header Save button via <Button form={id} type="submit">. */
  formId: string;
}

export function MetagenConfForm({
  initialValues,
  datasetFilter,
  onDatasetFilterChange,
  onSubmit,
  disabled = false,
  serverError,
  formId,
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
      name: initialValues?.name ?? "",
      is_enabled: initialValues?.is_enabled ?? false,
      schedule_tier: initialValues?.schedule_tier ?? null,
      result_limit: initialValues?.result_limit ?? 3,
      overwrite_pending: initialValues?.overwrite_pending ?? true,
    },
  });

  useEffect(() => {
    if (initialValues) {
      reset({
        name: initialValues.name,
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
      name: values.name,
      is_enabled: values.is_enabled,
      schedule_tier: values.schedule_tier ?? null,
      dataset_filter: datasetFilter as Record<string, unknown>,
      result_limit: values.result_limit,
      overwrite_pending: values.overwrite_pending,
    });
  }

  return (
    <form id={formId} onSubmit={handleSubmit(handleFormSubmit)} className="space-y-6">
      <Field
        label="name"
        htmlFor="metagen-conf-name"
        required
        error={errors.name?.message ?? serverError}
      >
        <Input
          id="metagen-conf-name"
          placeholder="catalog documentation policy"
          disabled={disabled}
          maxLength={200}
          {...register("name")}
        />
      </Field>

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
    </form>
  );
}
