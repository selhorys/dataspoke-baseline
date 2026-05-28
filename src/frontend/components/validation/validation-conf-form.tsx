"use client";

/**
 * ValidationConfForm — create or edit a validation config.
 *
 * Fields:
 *   description  — editable textarea, ≤ 2,000 chars
 *   variables[]  — field-array of named scalars; add / remove / rename
 *
 * Props:
 *   defaultValues  — initial form values
 *   onSubmit       — called with the serialized API request body
 *   onCancel       — optional cancel handler
 *   isPending      — loading state on Save button
 *   serverError?   — top-level error message from the mutation
 */

import { useFieldArray, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field } from "@/components/forms/field";
import { ErrorText } from "@/components/forms/error-text";
import type { ValidationConfFormValues } from "@/types/validation";
import { validationConfSchema, fromInternal } from "./validation-conf-form.schema";

interface ValidationConfFormProps {
  defaultValues: ValidationConfFormValues;
  onSubmit: (body: Record<string, unknown>) => void;
  onCancel?: () => void;
  isPending: boolean;
  serverError?: string;
}

export function ValidationConfForm({
  defaultValues,
  onSubmit,
  onCancel,
  isPending,
  serverError,
}: ValidationConfFormProps) {
  const {
    register,
    handleSubmit,
    control,
    formState: { errors },
  } = useForm<ValidationConfFormValues>({
    resolver: zodResolver(validationConfSchema),
    defaultValues,
  });

  const { fields, append, remove } = useFieldArray({
    control,
    name: "variables",
  });

  const onValid = (data: ValidationConfFormValues) => {
    onSubmit(fromInternal(data));
  };

  const variablesError =
    typeof errors.variables === "object" && !Array.isArray(errors.variables)
      ? (errors.variables as { message?: string }).message
      : undefined;

  return (
    <form onSubmit={handleSubmit(onValid)} className="space-y-5">
      {/* description */}
      <Field
        label="description"
        htmlFor="validation-description"
        error={errors.description?.message}
        required
      >
        <Textarea
          id="validation-description"
          {...register("description")}
          placeholder="Daily row count plus key column means and null counts"
          rows={3}
          className="resize-y"
        />
        <p className="text-xs text-muted-foreground">
          ≤ 2,000 characters. Surfaced in the DataHub assertion detail UI.
        </p>
      </Field>

      {/* variables field-array */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium leading-none">
            variables <span className="ml-1 text-destructive">*</span>
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => append({ name: "" })}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add
          </Button>
        </div>

        {variablesError && <ErrorText message={variablesError} />}

        <div className="space-y-2">
          {fields.map((field, index) => {
            const fieldError = errors.variables?.[index]?.name?.message;
            return (
              <div key={field.id} className="flex items-start gap-2">
                <div className="flex-1">
                  <Input
                    {...register(`variables.${index}.name`)}
                    placeholder="row_cnt"
                    aria-label={`Variable name ${index + 1}`}
                    className={fieldError ? "border-destructive" : ""}
                  />
                  {fieldError && (
                    <p className="mt-1 text-xs text-destructive">{fieldError}</p>
                  )}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => remove(index)}
                  aria-label={`Remove variable ${index + 1}`}
                  className="mt-0.5 h-9 w-9 shrink-0 p-0"
                  disabled={fields.length === 1}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            );
          })}
        </div>

        <p className="text-xs text-muted-foreground">
          Each name must match{" "}
          <code className="font-mono">{"[a-z][a-z0-9_]{0,99}"}</code>.
          1–200 unique entries.
        </p>
      </div>

      {serverError && <ErrorText message={serverError} />}

      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
        )}
        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving..." : "Save"}
        </Button>
      </div>
    </form>
  );
}
