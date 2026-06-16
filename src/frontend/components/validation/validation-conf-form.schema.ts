/**
 * ValidationConfForm Zod schema and serialization helpers — extracted for testability.
 *
 * Mirrors src/api/schemas/validation.py field constraints:
 *   description:           ≤ 2,000 chars; no ASCII control characters except \t (0x09) and \n (0x0a).
 *   variables:             1–200 entries; each name unique.
 *   variables[].name:      matches \A[a-z][a-z0-9_]{0,99}\Z.
 *   variables[].description: required key, ≤ 200 chars, empty allowed,
 *                          no ASCII control characters except \t (0x09) and \n (0x0a).
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md, spec/feature/VALIDATION.md §Rule Configuration.
 */

import { z } from "zod";
import type { ValidationConfFormValues } from "@/types/validation";
import type { ValidationConfResponse } from "@/types/validation";

// Mirrors the backend regex: \A[a-z][a-z0-9_]{0,99}\Z
export const VARIABLE_NAME_RE = /^[a-z][a-z0-9_]{0,99}$/;

/**
 * Returns a human-readable error for an invalid variable name.
 * The regex allows: start with lowercase letter, followed by lowercase letters,
 * digits, or underscores, total length 1–100.
 */
export function variableNameError(name: string): string | null {
  if (!VARIABLE_NAME_RE.test(name)) {
    return "Must start with a lowercase letter, contain only [a-z0-9_], and be ≤ 100 chars";
  }
  return null;
}

// Reject ASCII control characters except \t (0x09) and \n (0x0a), plus DEL (0x7f).
const CONTROL_CHAR_RE = /[\x00-\x08\x0b-\x1f\x7f]/;

const variableItemSchema = z.object({
  name: z
    .string()
    .min(1, "Variable name is required")
    .regex(
      VARIABLE_NAME_RE,
      "Must start with a lowercase letter, contain only [a-z0-9_], and be ≤ 100 chars",
    ),
  description: z
    .string()
    .max(200, "Variable description must not exceed 200 characters")
    .refine(
      (v) => !CONTROL_CHAR_RE.test(v),
      "Variable description contains invalid control characters",
    ),
});

export const validationConfSchema = z
  .object({
    description: z
      .string()
      .max(2000, "Description must not exceed 2,000 characters")
      .refine(
        (v) => !CONTROL_CHAR_RE.test(v),
        "Description contains invalid control characters",
      ),
    variables: z
      .array(variableItemSchema)
      .min(1, "At least one variable is required")
      .max(200, "Variables must not exceed 200 entries"),
  })
  .superRefine((data, ctx) => {
    // Uniqueness check
    const names = data.variables.map((v) => v.name);
    const seen = new Set<string>();
    names.forEach((name, idx) => {
      if (seen.has(name)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: `Duplicate variable name: "${name}"`,
          path: ["variables", idx, "name"],
        });
      }
      seen.add(name);
    });
  });

// ── Serialization helpers ──────────────────────────────────────────────────────

/**
 * toInternal: convert a ValidationConfResponse into the form shape.
 */
export function toInternal(conf: ValidationConfResponse): ValidationConfFormValues {
  return {
    description: conf.description,
    variables: conf.variables.map((v) => ({
      name: v.name,
      description: v.description,
    })),
  };
}

/** Default blank form values for a new config. */
export function defaultFormValues(): ValidationConfFormValues {
  return {
    description: "",
    variables: [{ name: "", description: "" }],
  };
}

/**
 * fromInternal: convert the form shape into the API request body for PUT.
 * variables is an array of { name, description } objects.
 */
export function fromInternal(v: ValidationConfFormValues): Record<string, unknown> {
  return {
    description: v.description,
    variables: v.variables.map((item) => ({
      name: item.name,
      description: item.description,
    })),
  };
}
