"use client";

/**
 * OntogenConfForm — edit the singleton ontogen operational conf.
 * Fields: is_enabled, schedule_tier, dataset_filter, default_run_prompt.
 */

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Checkbox } from "@/components/ui/checkbox";
import { Field } from "@/components/forms/field";
import { FormGrid } from "@/components/ui/form-grid";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DatasetFilterEditor } from "@/components/dataset-filter-editor";
import type { OntogenConf, OntogenConfPutBody } from "@/types/ontogen";
import type { DatasetFilter } from "@/types/governance";

const confSchema = z.object({
  is_enabled: z.boolean(),
  schedule_tier: z.enum(["hourly", "daily", "weekly"]).nullable(),
  default_run_prompt: z.string().max(16000).nullable(),
});

type ConfFormValues = z.infer<typeof confSchema>;

interface OntogenConfFormProps {
  /** Initial conf values; null triggers the empty/create state. */
  initialValues: OntogenConf | null;
  /** Called with dataset_filter state alongside form values. */
  datasetFilter: DatasetFilter;
  onDatasetFilterChange: (v: DatasetFilter) => void;
  onSubmit: (body: OntogenConfPutBody) => void;
  disabled?: boolean;
  /** id wired to the top-right header Save button via <Button form={id} type="submit">. */
  formId: string;
}

export function OntogenConfForm({
  initialValues,
  datasetFilter,
  onDatasetFilterChange,
  onSubmit,
  disabled = false,
  formId,
}: OntogenConfFormProps) {
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
      default_run_prompt: initialValues?.default_run_prompt ?? null,
    },
  });

  useEffect(() => {
    if (initialValues) {
      reset({
        is_enabled: initialValues.is_enabled,
        schedule_tier: initialValues.schedule_tier ?? null,
        default_run_prompt: initialValues.default_run_prompt ?? null,
      });
    }
  }, [initialValues, reset]);

  const isEnabled = watch("is_enabled");
  const scheduleTier = watch("schedule_tier");

  function handleFormSubmit(values: ConfFormValues) {
    onSubmit({
      is_enabled: values.is_enabled,
      schedule_tier: values.schedule_tier ?? null,
      dataset_filter: datasetFilter as Record<string, unknown>,
      default_run_prompt: values.default_run_prompt || null,
    });
  }

  return (
    <form id={formId} onSubmit={handleSubmit(handleFormSubmit)} className="space-y-6">
      <FormGrid>
        <Field label="is_enabled" htmlFor="conf-is-enabled">
          <div className="flex items-center gap-2">
            <Checkbox
              id="conf-is-enabled"
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
          htmlFor="conf-schedule-tier"
          hint="Periodic re-inference cadence"
        >
          <Select
            value={scheduleTier || "none"}
            onValueChange={(v) =>
              setValue("schedule_tier", v === "none" ? null : (v as "hourly" | "daily" | "weekly"))
            }
            disabled={disabled}
          >
            <SelectTrigger id="conf-schedule-tier">
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

        <div className="sm:col-span-2">
          <DatasetFilterEditor
            value={datasetFilter}
            onChange={onDatasetFilterChange}
            disabled={disabled}
          />
        </div>

        <Field
          label="default_run_prompt"
          htmlFor="conf-prompt"
          hint="Default one-shot prompt for periodic runs and bodyless manual calls (max 16 KB)"
          error={errors.default_run_prompt?.message}
          className="sm:col-span-2"
        >
          <Textarea
            id="conf-prompt"
            rows={8}
            placeholder="Describe the ontology inference prompt…"
            disabled={disabled}
            className="font-mono text-xs"
            {...register("default_run_prompt")}
          />
        </Field>
      </FormGrid>
    </form>
  );
}
