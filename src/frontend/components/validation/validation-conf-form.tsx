"use client";

/**
 * ValidationConfForm — create or edit a validation config.
 *
 * Fields:
 *   description  — editable textarea, ≤ 2,000 chars
 *   variables[]  — field-array of named scalars; add / remove / rename
 *
 * Props:
 *   formId         — id assigned to the <form>; lets a submit button placed
 *                    elsewhere in the DOM drive submission via form={formId}
 *   defaultValues  — initial form values
 *   onSubmit       — called with the serialized API request body
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
  formId: string;
  defaultValues: ValidationConfFormValues;
  onSubmit: (body: Record<string, unknown>) => void;
  serverError?: string;
}

export function ValidationConfForm({
  formId,
  defaultValues,
  onSubmit,
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
    <form id={formId} onSubmit={handleSubmit(onValid)} className="space-y-5">
      {/* description */}
      <Field
        label="description"
        htmlFor="validation-description"
        error={errors.description?.message}
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
            onClick={() => append({ name: "", description: "" })}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add
          </Button>
        </div>

        {variablesError && <ErrorText message={variablesError} />}

        <div className="space-y-2">
          {fields.map((field, index) => {
            const nameError = errors.variables?.[index]?.name?.message;
            const descError = errors.variables?.[index]?.description?.message;
            return (
              <div key={field.id} className="flex items-start gap-2">
                <div className="w-1/3 min-w-0">
                  <Input
                    {...register(`variables.${index}.name`)}
                    placeholder="row_cnt"
                    aria-label={`Variable name ${index + 1}`}
                    className={nameError ? "border-destructive" : ""}
                  />
                  {nameError && (
                    <p className="mt-1 text-xs text-destructive">{nameError}</p>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <Input
                    {...register(`variables.${index}.description`)}
                    placeholder="Daily row count"
                    aria-label={`Variable description ${index + 1}`}
                    maxLength={200}
                    className={descError ? "border-destructive" : ""}
                  />
                  {descError && (
                    <p className="mt-1 text-xs text-destructive">{descError}</p>
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
          <code className="font-mono">{"[a-z][a-z0-9_]{0,99}"}</code> and be
          unique (1–200 entries). Each description may be left blank, ≤ 200
          characters.
        </p>
      </div>

      {serverError && <ErrorText message={serverError} />}
    </form>
  );
}
