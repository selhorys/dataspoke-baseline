"use client";

/**
 * BoundaryForm — edit the per-dataset metagen boundary.
 * Fields: is_enabled, allowed (checkbox list of AllowedKind values), owner.
 */

import { useEffect } from "react";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import type { AllowedKind, MetagenBoundary, MetagenBoundaryPutBody } from "@/types/metagen";

const ALLOWED_KINDS: AllowedKind[] = ["dataset.description", "column.description"];

const boundarySchema = z.object({
  is_enabled: z.boolean(),
  allowed: z.array(z.enum(["dataset.description", "column.description"])),
  owner: z.string(),
});

type BoundaryFormValues = z.infer<typeof boundarySchema>;

interface BoundaryFormProps {
  formId: string;
  initialValues: MetagenBoundary | null;
  onSubmit: (body: MetagenBoundaryPutBody) => void;
  disabled?: boolean;
}

export function BoundaryForm({
  formId,
  initialValues,
  onSubmit,
  disabled = false,
}: BoundaryFormProps) {
  const {
    control,
    handleSubmit,
    watch,
    setValue,
    reset,
  } = useForm<BoundaryFormValues>({
    resolver: zodResolver(boundarySchema),
    defaultValues: {
      is_enabled: initialValues?.is_enabled ?? false,
      allowed: initialValues?.allowed ?? [],
      owner: initialValues?.owner ?? "",
    },
  });

  useEffect(() => {
    if (initialValues) {
      reset({
        is_enabled: initialValues.is_enabled,
        allowed: initialValues.allowed,
        owner: initialValues.owner ?? "",
      });
    }
  }, [initialValues, reset]);

  const allowed = watch("allowed");

  function toggleKind(kind: AllowedKind) {
    const next = allowed.includes(kind)
      ? allowed.filter((k) => k !== kind)
      : [...allowed, kind];
    setValue("allowed", next);
  }

  function handleFormSubmit(values: BoundaryFormValues) {
    const owner = values.owner.trim();
    onSubmit({
      is_enabled: values.is_enabled,
      allowed: values.allowed as AllowedKind[],
      owner: owner === "" ? null : owner,
    });
  }

  return (
    <form
      id={formId}
      onSubmit={handleSubmit(handleFormSubmit)}
      className="space-y-4"
    >
      <Field label="is_enabled" htmlFor="boundary-is-enabled">
        <div className="flex items-center gap-2">
          <Controller
            control={control}
            name="is_enabled"
            render={({ field }) => (
              <Checkbox
                id="boundary-is-enabled"
                checked={field.value}
                onCheckedChange={(v) => field.onChange(!!v)}
                disabled={disabled}
              />
            )}
          />
          <span className="text-sm text-muted-foreground">
            Include this dataset in MetaGen runs
          </span>
        </div>
      </Field>

      <fieldset className="space-y-2 rounded-md border p-3" disabled={disabled}>
        <legend className="px-1 text-sm font-medium text-muted-foreground">
          allowed aspects
        </legend>
        {ALLOWED_KINDS.map((kind) => (
          <div key={kind} className="flex items-center gap-2">
            <Checkbox
              id={`boundary-allowed-${kind}`}
              checked={allowed.includes(kind)}
              onCheckedChange={() => toggleKind(kind)}
              disabled={disabled}
            />
            <label
              htmlFor={`boundary-allowed-${kind}`}
              className="cursor-pointer font-mono text-sm"
            >
              {kind}
            </label>
          </div>
        ))}
      </fieldset>

      <Field label="owner" htmlFor="boundary-owner">
        <Controller
          control={control}
          name="owner"
          render={({ field }) => (
            <Input
              id="boundary-owner"
              value={field.value}
              onChange={field.onChange}
              onBlur={field.onBlur}
              placeholder="(unset)"
              disabled={disabled}
            />
          )}
        />
      </Field>
    </form>
  );
}
