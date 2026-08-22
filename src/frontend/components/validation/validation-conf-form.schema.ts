/**
 * ValidationConfForm Zod schema and serialization helpers — extracted for testability.
 *
 * Mirrors src/api/schemas/validation.py field constraints:
 *   description:           ≤ 2,000 chars; no ASCII control characters except \t (0x09) and \n (0x0a).
 *   variables:             1–200 entries; each name unique.
 *   variables[].name:      matches \A[a-z][a-z0-9_]{0,99}\Z.
 *   variables[].description: required key, ≤ 200 chars, empty allowed,
 *                          no ASCII control characters except \t (0x09) and \n (0x0a).
 *   attribute:             {cadence_unit, cadence_offset} — always complete, replaced wholesale.
 *   parameter:             absent, or 1–200 entries under the same per-item rules as
 *                          variables in its own namespace. An explicit [] is rejected.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md, spec/feature/VALIDATION.md §Rule Configuration.
 */

import { z } from "zod";
import { METRIC_TIME_WINDOW_SEC_MAX } from "@/types/governance";
import type {
  ValidationConfFormValues,
  ValidationConfPutRequest,
  ValidationConfResponse,
} from "@/types/validation";

// Mirrors the backend regex: \A[a-z][a-z0-9_]{0,99}\Z
export const VARIABLE_NAME_RE = /^[a-z][a-z0-9_]{0,99}$/;

/** API defaults for the `attribute` section (src/shared/db/models.py). */
export const DEFAULT_CADENCE_UNIT = 86400;
export const DEFAULT_CADENCE_OFFSET = 0;

/**
 * Ten years in seconds — the ceiling on `cadence_unit` and on the product
 * `cadence_offset * cadence_unit`, which is the window shift the governance
 * `validation-score` measurer applies.
 *
 * Re-exported from `METRIC_TIME_WINDOW_SEC_MAX` rather than declared afresh:
 * this is the *same* bound, not a coincidentally equal one — the backend's
 * `ValidationAttribute` imports `MAX_TIME_WINDOW_SEC` from
 * `src/shared/metric_conf.py` for exactly these two fields, so a second literal
 * here would be a copy that could drift out from under it. The local name is
 * kept because this file's messages and inputs speak of a cadence, not a window.
 */
export const CADENCE_MAX_SEC = METRIC_TIME_WINDOW_SEC_MAX;

const NAME_RULE_MESSAGE =
  "Must start with a lowercase letter, contain only [a-z0-9_], and be ≤ 100 chars";

/**
 * Returns a human-readable error for an invalid variable name.
 * The regex allows: start with lowercase letter, followed by lowercase letters,
 * digits, or underscores, total length 1–100.
 */
export function variableNameError(name: string): string | null {
  if (!VARIABLE_NAME_RE.test(name)) {
    return NAME_RULE_MESSAGE;
  }
  return null;
}

// Reject ASCII control characters except \t (0x09) and \n (0x0a), plus DEL (0x7f).
const CONTROL_CHAR_RE = /[\x00-\x08\x0b-\x1f\x7f]/;

/**
 * One `{name, description}` entry. `variables` and `parameter` share the rules
 * but not the namespace, so each gets its own instance with its own noun in the
 * messages — a bad parameter must not be reported as a bad variable.
 */
function namedEntrySchema(label: string) {
  return z.object({
    name: z
      .string()
      .min(1, `${label} name is required`)
      .regex(VARIABLE_NAME_RE, NAME_RULE_MESSAGE),
    description: z
      .string()
      .max(200, `${label} description must not exceed 200 characters`)
      .refine(
        (v) => !CONTROL_CHAR_RE.test(v),
        `${label} description contains invalid control characters`,
      ),
  });
}

const variableItemSchema = namedEntrySchema("Variable");
const parameterItemSchema = namedEntrySchema("Parameter");

/**
 * A cadence integer. `valueAsNumber` on an emptied number input yields `NaN`,
 * which `z.number()` reports as an invalid type — hence the explicit message,
 * so the user reads "required" rather than "expected number, received nan".
 */
function cadenceField(label: string) {
  return z
    .number({ invalid_type_error: `${label} is required` })
    .int(`${label} must be a whole number of seconds`);
}

const attributeSchema = z
  .object({
    cadence_unit: cadenceField("cadence_unit")
      .min(1, "cadence_unit must be greater than 0")
      .max(CADENCE_MAX_SEC, `cadence_unit must not exceed ${CADENCE_MAX_SEC} seconds (ten years)`),
    cadence_offset: cadenceField("cadence_offset").min(0, "cadence_offset must not be negative"),
  })
  .superRefine((attribute, ctx) => {
    // The product, not either factor, is what bounds the measurer's window
    // shift — cadence_offset carries no ceiling of its own.
    if (attribute.cadence_offset * attribute.cadence_unit > CADENCE_MAX_SEC) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `cadence_offset × cadence_unit must not exceed ${CADENCE_MAX_SEC} seconds (ten years)`,
        path: ["cadence_offset"],
      });
    }
  });

/** Flags each repeat of an already-seen name, on that row's `name` path. */
function checkUniqueNames(
  entries: ReadonlyArray<{ name: string }>,
  field: "variables" | "parameter",
  noun: string,
  ctx: z.RefinementCtx,
): void {
  const seen = new Set<string>();
  entries.forEach((entry, idx) => {
    if (seen.has(entry.name)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `Duplicate ${noun} name: "${entry.name}"`,
        path: [field, idx, "name"],
      });
    }
    seen.add(entry.name);
  });
}

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
    attribute: attributeSchema,
    // No lower bound: an empty list is the form's spelling of "section absent",
    // and fromInternal omits the key rather than sending the `[]` the API rejects.
    parameter: z.array(parameterItemSchema).max(200, "Parameters must not exceed 200 entries"),
  })
  .superRefine((data, ctx) => {
    // Uniqueness is per list — the same name may appear in both.
    checkUniqueNames(data.variables, "variables", "variable", ctx);
    checkUniqueNames(data.parameter, "parameter", "parameter", ctx);
  });

// ── Serialization helpers ──────────────────────────────────────────────────────

/**
 * toInternal: convert a ValidationConfResponse into the form shape.
 *
 * An absent `parameter` (the key is omitted from the response, never null)
 * becomes the empty list — the form's representation of the absent section.
 */
export function toInternal(conf: ValidationConfResponse): ValidationConfFormValues {
  return {
    description: conf.description,
    variables: conf.variables.map((v) => ({
      name: v.name,
      description: v.description,
    })),
    attribute: {
      cadence_unit: conf.attribute.cadence_unit,
      cadence_offset: conf.attribute.cadence_offset,
    },
    parameter: (conf.parameter ?? []).map((p) => ({
      name: p.name,
      description: p.description,
    })),
  };
}

/** Default blank form values for a new config, pre-filled with the API defaults. */
export function defaultFormValues(): ValidationConfFormValues {
  return {
    description: "",
    variables: [{ name: "", description: "" }],
    attribute: {
      cadence_unit: DEFAULT_CADENCE_UNIT,
      cadence_offset: DEFAULT_CADENCE_OFFSET,
    },
    parameter: [],
  };
}

/**
 * fromInternal: convert the form shape into the API request body for PUT.
 *
 * `attribute` always goes out complete — PUT replaces it wholesale, so there is
 * no partial form of it. `parameter` is omitted when the list is empty: on PUT
 * an omitted key stores the section as absent, while an explicit `[]` is
 * rejected with 422.
 */
export function fromInternal(v: ValidationConfFormValues): ValidationConfPutRequest {
  const body: ValidationConfPutRequest = {
    description: v.description,
    variables: v.variables.map((item) => ({
      name: item.name,
      description: item.description,
    })),
    attribute: {
      cadence_unit: v.attribute.cadence_unit,
      cadence_offset: v.attribute.cadence_offset,
    },
  };
  if (v.parameter.length > 0) {
    body.parameter = v.parameter.map((item) => ({
      name: item.name,
      description: item.description,
    }));
  }
  return body;
}
