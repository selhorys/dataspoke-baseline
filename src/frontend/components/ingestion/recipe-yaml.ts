/**
 * Lossless JSON⇄YAML transforms and validation for ingestion source bodies.
 *
 * The recipe `${name__key}` secret references round-trip verbatim (the API
 * never returns plaintext secrets, so "masking" = preserving the refs plus a
 * visual highlight in the editor). All transforms preserve structure and null
 * schedule.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail / §Create View,
 *       src/api/schemas/ingestion.py, src/shared/models/ingestion.py.
 */

import { parse, parseDocument, stringify } from "yaml";
import { z } from "zod";
import type {
  IngestionMode,
  IngestionSource,
  IngestionSourceBody,
} from "@/types/ingestion";

/** The editable subset of a source, in canonical field order. */
export function toEditableBody(source: IngestionSource): IngestionSourceBody {
  return {
    mode: source.mode,
    name: source.name,
    schedule: source.schedule,
    recipe: source.recipe,
  };
}

/**
 * Serialise an editable source body to YAML. Only the four author-controlled
 * fields {mode, name, schedule, recipe} are emitted — read-only fields (id,
 * platform, status, datahub_source_urn, created_at, updated_at, resp_time) are
 * never included. Null schedule is preserved as an explicit `null`.
 */
export function sourceBodyToYaml(
  source: IngestionSource | IngestionSourceBody,
): string {
  const body: IngestionSourceBody = {
    mode: source.mode,
    name: source.name,
    schedule: source.schedule,
    recipe: source.recipe,
  };
  return stringify(body, { nullStr: "null" });
}

export interface ParseResult {
  ok: boolean;
  /** Parsed value when ok; otherwise undefined. */
  value?: unknown;
  /** Human-readable error with a 1-based line number when ok is false. */
  error?: string;
}

/**
 * Parse YAML text into a JS value, reporting parse errors inline with a
 * 1-based line number via the `yaml` document's `linePos`.
 */
export function parseSourceYaml(text: string): ParseResult {
  if (!text.trim()) {
    return { ok: false, error: "Recipe body is empty." };
  }
  const doc = parseDocument(text);
  if (doc.errors.length > 0) {
    const err = doc.errors[0];
    const linePos = err.linePos?.[0];
    const where = linePos ? ` (line ${linePos.line})` : "";
    return { ok: false, error: `${err.message}${where}` };
  }
  try {
    const value = parse(text);
    return { ok: true, value };
  } catch (e) {
    return {
      ok: false,
      error: e instanceof Error ? e.message : "Failed to parse YAML.",
    };
  }
}

// ── Validation ───────────────────────────────────────────────────────────────────

// Canonical crons accepted for ACTIVE_CUSTOM_MANAGED schedules — mirrors
// CRON_TO_TIER in src/shared/models/ingestion.py.
const CANONICAL_CRONS = new Set([
  "0 * * * *",
  "@hourly",
  "0 0 * * *",
  "@daily",
  "@midnight",
  "0 0 * * 0",
  "@weekly",
]);

const recipeSchema = z
  .object({
    source: z
      .object({
        type: z.string().min(1, "recipe.source.type must be a non-empty string"),
        config: z.record(z.unknown()).optional(),
      })
      .passthrough(),
  })
  .passthrough();

export interface ValidateOptions {
  /** When true, DATAHUB_MANAGED is rejected (create flow). */
  creatableOnly?: boolean;
  /**
   * When true, only the `recipe` portion is validated; mode/name/schedule are
   * not required. Used by the create flow, where the page owns those fields and
   * the editor manages recipe text only.
   */
  recipeOnly?: boolean;
}

export interface ValidationResult {
  ok: boolean;
  body?: IngestionSourceBody;
  /** Populated for `recipeOnly` validation — the validated recipe object. */
  recipe?: Record<string, unknown>;
  error?: string;
}

/**
 * Validate a parsed source body against the shape constraints mirrored from
 * the API request schemas. Semantic verification (secret-ref existence) is the
 * server's job; this is shape-only.
 *
 * With `recipeOnly`, only the recipe shape is checked — the parsed YAML is
 * treated as a bare recipe object (the create page owns mode/name/schedule and
 * runs the full validation on the composed body before POST).
 */
export function validateSourceBody(
  raw: unknown,
  opts: ValidateOptions = {},
): ValidationResult {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, error: "Recipe body must be a YAML mapping." };
  }
  const obj = raw as Record<string, unknown>;

  if (opts.recipeOnly) {
    const recipeParse = recipeSchema.safeParse(obj);
    if (!recipeParse.success) {
      const first = recipeParse.error.issues[0];
      const path = first.path.join(".");
      return {
        ok: false,
        error: path ? `recipe.${path}: ${first.message}` : first.message,
      };
    }
    return { ok: true, recipe: obj };
  }

  const mode = obj.mode;
  if (
    mode !== "DATAHUB_MANAGED" &&
    mode !== "ACTIVE_CUSTOM_MANAGED" &&
    mode !== "PASSIVE"
  ) {
    return {
      ok: false,
      error:
        "mode must be one of DATAHUB_MANAGED, ACTIVE_CUSTOM_MANAGED, PASSIVE.",
    };
  }
  if (opts.creatableOnly && mode === "DATAHUB_MANAGED") {
    return {
      ok: false,
      error: "DATAHUB_MANAGED sources are synced from DataHub, not creatable.",
    };
  }

  const name = obj.name;
  if (typeof name !== "string" || name.length < 1 || name.length > 512) {
    return { ok: false, error: "name must be a string of 1–512 characters." };
  }

  // schedule: null/absent allowed for all; canonical cron only for ACTIVE.
  const schedule = obj.schedule ?? null;
  if (schedule !== null) {
    if (typeof schedule !== "string") {
      return { ok: false, error: "schedule must be a cron string or null." };
    }
    if (mode === "PASSIVE") {
      return {
        ok: false,
        error: "PASSIVE sources must not carry a schedule.",
      };
    }
    if (mode === "ACTIVE_CUSTOM_MANAGED" && !CANONICAL_CRONS.has(schedule.trim())) {
      return {
        ok: false,
        error:
          "schedule must be null or one of the canonical hourly / daily / weekly crons.",
      };
    }
  }

  const recipeParse = recipeSchema.safeParse(obj.recipe);
  if (!recipeParse.success) {
    const first = recipeParse.error.issues[0];
    const path = first.path.join(".");
    return {
      ok: false,
      error: path ? `recipe.${path}: ${first.message}` : first.message,
    };
  }

  return {
    ok: true,
    body: {
      mode: mode as IngestionMode,
      name,
      schedule: schedule as string | null,
      recipe: obj.recipe as Record<string, unknown>,
    },
  };
}

// ── Secret references ────────────────────────────────────────────────────────────

const SECRET_REF_RE = /\$\{[^}]*__[^}]*\}/g;

/** Return the distinct `${name__key}` tokens present anywhere in the text. */
export function findSecretRefs(text: string): string[] {
  const matches = text.match(SECRET_REF_RE);
  if (!matches) return [];
  return Array.from(new Set(matches));
}
