"use client";

/**
 * ValidationConfForm — create or edit a validation config.
 *
 * Fields (the conf's four sections):
 *   description  — editable textarea, ≤ 2,000 chars
 *   variables[]  — field-array of named scalars; add / remove / rename
 *   attribute    — Data arrival: cadence_unit / cadence_offset number inputs
 *   parameter[]  — optional field-array, same row editor as variables
 *
 * Props:
 *   formId         — id assigned to the <form>; lets a submit button placed
 *                    elsewhere in the DOM drive submission via form={formId}
 *   defaultValues  — initial form values
 *   onSubmit       — called with the serialized API request body
 *   serverError?   — top-level error message from the mutation
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md, spec/feature/VALIDATION.md §Rule Configuration.
 */

import type { ReactNode } from "react";
import {
  useFieldArray,
  useForm,
  type Control,
  type FieldErrors,
  type UseFormRegister,
} from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field } from "@/components/forms/field";
import { ErrorText } from "@/components/forms/error-text";
import type {
  ValidationConfFormValues,
  ValidationConfPutRequest,
} from "@/types/validation";
import {
  validationConfSchema,
  fromInternal,
  CADENCE_MAX_SEC,
} from "./validation-conf-form.schema";

/** The two `{name, description}` field-arrays the row editor below serves. */
type EntryFieldName = "variables" | "parameter";

interface NamedEntryEditorProps {
  /** Which conf section this editor edits. */
  name: EntryFieldName;
  /** Section heading, e.g. "variables". */
  label: string;
  /** Singular noun for the per-row aria-labels ("variable" / "parameter"). */
  noun: string;
  /** `variables` needs ≥ 1 row; `parameter` may be emptied back to absent. */
  minRows: number;
  namePlaceholder: string;
  descriptionPlaceholder: string;
  /** Rendered when the list is empty (only reachable for `parameter`). */
  emptyHint?: string;
  /**
   * Accessible name for the section's Add button. Both lists render the same
   * visible "Add", so each names what it appends — two bare "Add" buttons in
   * one form are indistinguishable to a screen reader.
   */
  addAriaLabel: string;
  hint: ReactNode;
  control: Control<ValidationConfFormValues>;
  register: UseFormRegister<ValidationConfFormValues>;
  errors: FieldErrors<ValidationConfFormValues>;
}

/**
 * The shared row editor behind both `variables` and `parameter`: a `name` input,
 * a `description` input, an `[×]` per row and a `[+ Add]` for the section. The
 * two lists carry the same per-item rules, so they carry the same control —
 * `minRows` is the only behavioural difference (the last `variables` row cannot
 * be removed; the last `parameter` row can, returning the section to absent).
 */
function NamedEntryEditor({
  name,
  label,
  noun,
  minRows,
  namePlaceholder,
  descriptionPlaceholder,
  emptyHint,
  addAriaLabel,
  hint,
  control,
  register,
  errors,
}: NamedEntryEditorProps) {
  const { fields, append, remove } = useFieldArray({ control, name });

  // "variable" → "Variable", for the sentence-cased per-row aria-labels.
  const Noun = noun.charAt(0).toUpperCase() + noun.slice(1);

  // A root-level array error (count bounds) is an object with `message`; per-row
  // errors arrive as an array. Both live at the same key, so discriminate first.
  const arrayErrors = errors[name];
  const rootError = Array.isArray(arrayErrors) ? undefined : arrayErrors?.message;
  const rowError = (index: number, key: "name" | "description"): string | undefined =>
    Array.isArray(arrayErrors) ? arrayErrors[index]?.[key]?.message : undefined;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium leading-none">
          {label}
          {minRows > 0 && <span className="ml-1 text-destructive">*</span>}
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={addAriaLabel}
          onClick={() => append({ name: "", description: "" })}
        >
          <Plus className="mr-1 h-3.5 w-3.5" />
          Add
        </Button>
      </div>

      {rootError && <ErrorText message={rootError} />}

      {fields.length === 0 && emptyHint && (
        <p className="text-xs text-muted-foreground">{emptyHint}</p>
      )}

      <div className="space-y-2">
        {fields.map((field, index) => {
          const nameError = rowError(index, "name");
          const descError = rowError(index, "description");
          return (
            <div key={field.id} className="flex items-start gap-2">
              <div className="w-1/3 min-w-0">
                <Input
                  {...register(`${name}.${index}.name` as const)}
                  placeholder={namePlaceholder}
                  aria-label={`${Noun} name ${index + 1}`}
                  className={nameError ? "border-destructive" : ""}
                />
                {nameError && <p className="mt-1 text-xs text-destructive">{nameError}</p>}
              </div>
              <div className="flex-1 min-w-0">
                <Input
                  {...register(`${name}.${index}.description` as const)}
                  placeholder={descriptionPlaceholder}
                  aria-label={`${Noun} description ${index + 1}`}
                  maxLength={200}
                  className={descError ? "border-destructive" : ""}
                />
                {descError && <p className="mt-1 text-xs text-destructive">{descError}</p>}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => remove(index)}
                aria-label={`Remove ${noun} ${index + 1}`}
                className="mt-0.5 h-9 w-9 shrink-0 p-0"
                disabled={fields.length <= minRows}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          );
        })}
      </div>

      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}

interface ValidationConfFormProps {
  formId: string;
  defaultValues: ValidationConfFormValues;
  onSubmit: (body: ValidationConfPutRequest) => void;
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

  const onValid = (data: ValidationConfFormValues) => {
    onSubmit(fromInternal(data));
  };

  const nameRuleHint = (
    <>
      Each name must match <code className="font-mono">{"[a-z][a-z0-9_]{0,99}"}</code> and be
      unique (1–200 entries). Each description may be left blank, ≤ 200 characters.
    </>
  );

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
      <NamedEntryEditor
        name="variables"
        label="variables"
        noun="variable"
        minRows={1}
        namePlaceholder="row_cnt"
        descriptionPlaceholder="Daily row count"
        addAriaLabel="Add variable"
        hint={nameRuleHint}
        control={control}
        register={register}
        errors={errors}
      />

      {/* attribute — the conf's declared data-arrival cadence */}
      <div className="space-y-2">
        <span className="text-sm font-medium leading-none">Data arrival</span>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label="cadence_unit"
            htmlFor="validation-cadence-unit"
            description="Seconds between expected arrivals. Daily data is 86,400."
            error={errors.attribute?.cadence_unit?.message}
          >
            <Input
              id="validation-cadence-unit"
              type="number"
              min={1}
              max={CADENCE_MAX_SEC}
              step={1}
              {...register("attribute.cadence_unit", { valueAsNumber: true })}
            />
          </Field>
          <Field
            label="cadence_offset"
            htmlFor="validation-cadence-offset"
            description="How many periods the data lags. D-1 daily is 0, D-8 daily is 7."
            error={errors.attribute?.cadence_offset?.message}
          >
            <Input
              id="validation-cadence-offset"
              type="number"
              min={0}
              step={1}
              {...register("attribute.cadence_offset", { valueAsNumber: true })}
            />
          </Field>
        </div>
        <p className="text-xs text-muted-foreground">
          The pair anchors the window the governance <code className="font-mono">validation-score</code>{" "}
          metric judges this dataset against.
        </p>
      </div>

      {/* parameter field-array — optional, absent when empty */}
      <NamedEntryEditor
        name="parameter"
        label="parameters"
        noun="parameter"
        minRows={0}
        namePlaceholder="z_threshold"
        descriptionPlaceholder="Std-dev cutoff for outliers"
        emptyHint="None declared — the section is omitted from the config until a row is added."
        addAriaLabel="Add parameter"
        hint={
          <>
            Optional hyperparameters for the pipeline&apos;s own use; DataSpoke stores them
            without interpreting them. Same rules as variables, in a separate namespace.
          </>
        }
        control={control}
        register={register}
        errors={errors}
      />

      {serverError && <ErrorText message={serverError} />}
    </form>
  );
}
