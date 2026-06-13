"use client";

/**
 * RecipeYamlEditor — view/edit an ingestion source body as YAML.
 *
 * Read-only mode renders a <pre> with `${name__key}` secret references visually
 * highlighted (the API never returns plaintext, so this is the masked form).
 * Editable mode shows a monospace Textarea with Save/Cancel and inline parse +
 * zod-shape errors. Server 422/409 are echoed inline as `error_code: message`.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Recipe.
 */

import { Fragment, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ErrorText } from "@/components/forms/error-text";
import {
  parseSourceYaml,
  validateSourceBody,
  findSecretRefs,
  type ValidateOptions,
} from "./recipe-yaml";
import type { IngestionSourceBody } from "@/types/ingestion";

interface RecipeYamlEditorProps {
  /** Initial YAML text. */
  value: string;
  /** Read-only view (DATAHUB_MANAGED or Reader). */
  readOnly?: boolean;
  /** When editing, controls whether the Save/Cancel chrome is shown. */
  editing?: boolean;
  onEditRequest?: () => void;
  onCancel?: () => void;
  onSave?: (body: IngestionSourceBody, yamlText: string) => void;
  /**
   * Recipe-only save callback, used when `validateOptions.recipeOnly` is set:
   * the editor validates the recipe shape only and hands the parsed recipe back
   * to the page, which owns mode/name/schedule and runs the full validation.
   */
  onRecipeSave?: (recipe: Record<string, unknown>, yamlText: string) => void;
  isSaving?: boolean;
  /** Server error to echo inline (e.g. "INGESTION_SOURCE_READONLY: ..."). */
  serverError?: string;
  validateOptions?: ValidateOptions;
}

const SECRET_REF_SPLIT_RE = /(\$\{[^}]*__[^}]*\})/g;
// Non-global, anchored test regex — `.test()` on a /g regex is stateful and
// would alternate results across calls, so keep a separate matcher here.
const SECRET_REF_TEST_RE = /^\$\{[^}]*__[^}]*\}$/;

/** Render YAML text with secret refs highlighted. */
function HighlightedYaml({ text }: { text: string }) {
  const parts = text.split(SECRET_REF_SPLIT_RE);
  return (
    <pre className="overflow-auto rounded-md border bg-muted/40 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap">
      {parts.map((part, i) =>
        SECRET_REF_TEST_RE.test(part) ? (
          <span
            key={i}
            className="rounded bg-amber-500/20 px-0.5 font-semibold text-amber-700 dark:text-amber-400"
            title="Secret reference — resolved server-side, never plaintext"
          >
            {part}
          </span>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </pre>
  );
}

export function RecipeYamlEditor({
  value,
  readOnly = false,
  editing = false,
  onEditRequest,
  onCancel,
  onSave,
  onRecipeSave,
  isSaving = false,
  serverError,
  validateOptions,
}: RecipeYamlEditorProps) {
  const [text, setText] = useState(value);
  const [clientError, setClientError] = useState<string | undefined>();

  useEffect(() => {
    setText(value);
    setClientError(undefined);
  }, [value, editing]);

  // ── Read-only view ─────────────────────────────────────────────────────────
  if (readOnly || !editing) {
    return (
      <div className="space-y-2">
        {!readOnly && onEditRequest && (
          <div className="flex justify-end">
            <Button size="sm" variant="outline" onClick={onEditRequest}>
              Edit
            </Button>
          </div>
        )}
        <HighlightedYaml text={value} />
      </div>
    );
  }

  // ── Editable view ──────────────────────────────────────────────────────────
  const refs = findSecretRefs(text);

  function handleSave() {
    const parsed = parseSourceYaml(text);
    if (!parsed.ok) {
      setClientError(parsed.error);
      return;
    }
    const result = validateSourceBody(parsed.value, validateOptions);
    if (!result.ok) {
      setClientError(result.error);
      return;
    }
    if (validateOptions?.recipeOnly) {
      if (!result.recipe) {
        setClientError("Recipe is invalid.");
        return;
      }
      setClientError(undefined);
      onRecipeSave?.(result.recipe, text);
      return;
    }
    if (!result.body) {
      setClientError(result.error);
      return;
    }
    setClientError(undefined);
    onSave?.(result.body, text);
  }

  return (
    <div className="space-y-3">
      <Textarea
        aria-label="recipe YAML"
        rows={18}
        className="font-mono text-xs"
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={isSaving}
        spellCheck={false}
      />
      {refs.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Secret refs:{" "}
          {refs.map((r) => (
            <span
              key={r}
              className="mr-1 rounded bg-amber-500/20 px-1 font-mono font-semibold text-amber-700 dark:text-amber-400"
            >
              {r}
            </span>
          ))}
        </p>
      )}
      <ErrorText message={clientError ?? serverError} />
      <div className="flex gap-2">
        <Button onClick={handleSave} disabled={isSaving}>
          {isSaving ? "Saving…" : "Save"}
        </Button>
        {onCancel && (
          <Button variant="outline" onClick={onCancel} disabled={isSaving}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  );
}
