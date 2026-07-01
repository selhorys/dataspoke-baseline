"use client";

/**
 * BoundaryForm — edit the per-dataset metagen boundary.
 * Fields: is_enabled, allowed (checkbox list of AllowedKind values).
 */

import { useEffect } from "react";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Checkbox } from "@/components/ui/checkbox";
import type { AllowedKind, MetagenBoundary, MetagenBoundaryPutBody } from "@/types/metagen";

const ALLOWED_KINDS: AllowedKind[] = ["dataset.description", "column.description"];

const boundarySchema = z.object({
  is_enabled: z.boolean(),
  allowed: z.array(z.enum(["dataset.description", "column.description"])),
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
    },
  });

  useEffect(() => {
    if (initialValues) {
      reset({
        is_enabled: initialValues.is_enabled,
        allowed: initialValues.allowed,
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
    onSubmit({
      is_enabled: values.is_enabled,
      allowed: values.allowed as AllowedKind[],
    });
  }

  return (
    <form
      id={formId}
      onSubmit={handleSubmit(handleFormSubmit)}
      className="space-y-4"
    >
      <div className="grid grid-cols-2 gap-3">
        <fieldset className="space-y-2 rounded-md border p-3" disabled={disabled}>
          <legend className="px-1 text-sm font-medium text-muted-foreground">
            is_enabled
          </legend>
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
            <label
              htmlFor="boundary-is-enabled"
              className="cursor-pointer text-sm text-muted-foreground"
            >
              Include this dataset in MetaGen runs
            </label>
          </div>
        </fieldset>

        <fieldset className="space-y-2 rounded-md border p-3" disabled={disabled}>
          <legend className="px-1 text-sm font-medium text-muted-foreground">
            allowed
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
      </div>
    </form>
  );
}
