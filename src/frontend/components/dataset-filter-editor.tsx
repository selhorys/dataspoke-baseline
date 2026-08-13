"use client";

/**
 * DatasetFilterEditor — the shared `dataset_filter` editor.
 *
 * Used by: Governance metric create/edit, OntoGen config, MetaGen config.
 *
 * `dataset_filter` is a SQL `WHERE`-clause string over the dataset registry
 * (spec/API.md §`dataset_filter` grammar); the empty string matches every
 * registered dataset. The editor is one vertically resizable monospace textarea
 * holding the clause verbatim — it parses nothing and never re-serialises
 * derived state back into the box.
 *
 * The **Auto-indent** button reformats the text in place through the purely
 * lexical formatter in lib/dataset-filter-format.ts: it holds no grammar
 * knowledge and so never rejects, rewrites, or silently repairs a clause it
 * cannot understand. Validation is server-side — a `422 INVALID_DATASET_FILTER`
 * comes back with `detail.position` and renders inline against the field
 * (`error` prop). A folded grammar guide sits beneath the box.
 *
 * The box reseeds from props only when the incoming filter is not the one this
 * editor last emitted (e.g. a freshly loaded record), so text the user is
 * mid-way through typing survives a parent re-render.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shared component notes.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { DatasetFilterGuide } from "@/components/dataset-filter-guide";
import { formatDatasetFilter } from "@/lib/dataset-filter-format";
import type { DatasetFilterErrorInfo } from "@/lib/dataset-filter-error";

export interface DatasetFilterEditorProps {
  /** The clause text. */
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  /** Filter error (server 422 or a form-side cap), rendered inline against the field. */
  error?: DatasetFilterErrorInfo;
  /** Textarea id — distinct ids let two editors coexist on one page. */
  id?: string;
}

const PLACEHOLDER =
  "origin = 'PROD'\nAND 'urn:li:tag:area:catalog' IN tag_urns";

export function DatasetFilterEditor({
  value,
  onChange,
  disabled = false,
  error,
  id = "dataset-filter",
}: DatasetFilterEditorProps) {
  const [text, setText] = useState<string>(value ?? "");

  // The clause the box is currently in sync with — either the last value this
  // editor emitted or the last one it reseeded from.
  const syncedRef = useRef<string>(value ?? "");

  useEffect(() => {
    const incoming = value ?? "";
    if (incoming === syncedRef.current) return;
    syncedRef.current = incoming;
    setText(incoming);
  }, [value]);

  const emit = useCallback(
    (next: string) => {
      syncedRef.current = next;
      setText(next);
      onChange(next);
    },
    [onChange],
  );

  const handleAutoIndent = useCallback(() => {
    emit(formatDatasetFilter(text));
  }, [emit, text]);

  return (
    <fieldset className="space-y-3 rounded-md border p-4" disabled={disabled}>
      <legend className="px-1 text-sm font-medium text-muted-foreground">dataset_filter</legend>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          SQL <span className="font-mono">WHERE</span> clause over the dataset registry — an
          empty filter matches every registered dataset.
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleAutoIndent}
          disabled={disabled}
        >
          Auto-indent
        </Button>
      </div>

      <Textarea
        id={id}
        aria-label="dataset_filter"
        rows={6}
        spellCheck={false}
        value={text}
        onChange={(e) => emit(e.target.value)}
        placeholder={PLACEHOLDER}
        disabled={disabled}
        className="min-h-[7rem] resize-y font-mono text-xs"
      />

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error.message}
          {error.position !== undefined && (
            <span className="ml-1 font-mono text-xs">(character {error.position})</span>
          )}
        </p>
      )}

      <DatasetFilterGuide />
    </fieldset>
  );
}
