"use client";

/**
 * DatasetFilterEditor — reusable four-dimension dataset filter form section.
 *
 * Used by: Governance metric create/edit, OntoGen config, MetaGen config.
 *
 * Props contract:
 *   value: DatasetFilter           — current filter state
 *   onChange: (v: DatasetFilter) => void — called on any field change
 *   disabled?: boolean             — makes all fields read-only
 *
 * dataset_filter shape (mirrors src/api/schemas/_dataset_filter.py):
 *   origin          — optional string (DataHub FabricType, AND-ed with OR-group)
 *   tags            — optional string[] (tag URNs, OR-ed)
 *   glossary_terms  — optional string[] (glossary term URNs, OR-ed)
 *   dataset_urns    — optional string[] (explicit dataset URNs, OR-ed)
 *
 * Input contract (spec/feature/FRONTEND_BASIC.md §Shared component notes): each
 * list dimension is one newline-separated textarea holding the raw text the user
 * typed. Parsing happens on the way out — one URN per line, each line
 * edge-trimmed, blank lines dropped, an empty dimension emitted as undefined —
 * and parsed state is never re-serialised back into the box, so whitespace the
 * user is mid-way through typing survives. Commas are not separators: tag and
 * glossary-term URNs embed a user-authored name that may contain a comma, and a
 * dataset URN always contains them inside its (platform,name,fabric) tuple.
 * Boxes are reseeded from props only when the incoming filter is not the one
 * this editor last emitted (e.g. a freshly loaded record).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Field } from "@/components/forms/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { DatasetFilter } from "@/types/governance";

export interface DatasetFilterEditorProps {
  value: DatasetFilter;
  onChange: (v: DatasetFilter) => void;
  disabled?: boolean;
}

/** The three list dimensions of dataset_filter (origin is a scalar). */
type ListDimension = "tags" | "glossary_terms" | "dataset_urns";

const LIST_DIMENSIONS: readonly ListDimension[] = ["tags", "glossary_terms", "dataset_urns"];

/**
 * splitList — the single textarea-to-array parser for every URN list input.
 * Splits on newline only (CRLF tolerated), edge-trims each line and drops blank
 * lines; whitespace inside a line is preserved.
 */
export function splitList(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function joinList(arr: string[] | undefined): string {
  return (arr ?? []).join("\n");
}

/** Element-wise list compare; absent and empty are the same list. */
function sameList(a: string[] | undefined, b: string[] | undefined): boolean {
  if (a === b) return true;
  if (!a || !b) return (a?.length ?? 0) === 0 && (b?.length ?? 0) === 0;
  return a.length === b.length && a.every((entry, i) => entry === b[i]);
}

type RawText = Record<ListDimension, string>;

function seedRaw(value: DatasetFilter): RawText {
  return {
    tags: joinList(value.tags),
    glossary_terms: joinList(value.glossary_terms),
    dataset_urns: joinList(value.dataset_urns),
  };
}

export function DatasetFilterEditor({ value, onChange, disabled = false }: DatasetFilterEditorProps) {
  const [raw, setRaw] = useState<RawText>(() => seedRaw(value));

  // The filter the textareas are currently in sync with — either the last value
  // this editor emitted or the last one it reseeded from.
  const syncedRef = useRef<DatasetFilter>(value);

  // Reseed only the dimensions the parent changed behind our back (a freshly
  // loaded record). Echoes of our own emissions leave the raw text untouched.
  useEffect(() => {
    const synced = syncedRef.current;
    syncedRef.current = value;
    const stale = LIST_DIMENSIONS.filter((dim) => !sameList(value[dim], synced[dim]));
    if (stale.length === 0) return;
    setRaw((prev) => {
      const next = { ...prev };
      for (const dim of stale) next[dim] = joinList(value[dim]);
      return next;
    });
  }, [value]);

  const emit = useCallback(
    (next: DatasetFilter) => {
      syncedRef.current = next;
      onChange(next);
    },
    [onChange],
  );

  const setListDimension = useCallback(
    (dim: ListDimension, text: string) => {
      setRaw((prev) => ({ ...prev, [dim]: text }));
      const list = splitList(text);
      emit({ ...value, [dim]: list.length ? list : undefined });
    },
    [value, emit],
  );

  const set = useCallback(
    (patch: Partial<DatasetFilter>) => emit({ ...value, ...patch }),
    [value, emit],
  );

  return (
    <fieldset className="space-y-4 rounded-md border p-4" disabled={disabled}>
      <legend className="px-1 text-sm font-medium text-muted-foreground">dataset_filter</legend>

      <Field
        label="origin"
        htmlFor="df-origin"
        hint="DataHub FabricType — AND-ed with the tag/term/URN group (e.g. DEV, PROD)"
      >
        <Input
          id="df-origin"
          value={value.origin ?? ""}
          onChange={(e) => set({ origin: e.target.value || undefined })}
          placeholder="DEV"
          disabled={disabled}
        />
      </Field>

      <Field
        label="tags"
        htmlFor="df-tags"
        hint="Tag URNs, one per line — blank lines dropped, each line edge-trimmed (OR-ed)"
      >
        <Textarea
          id="df-tags"
          rows={3}
          value={raw.tags}
          onChange={(e) => setListDimension("tags", e.target.value)}
          placeholder="urn:li:tag:area:catalog"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>

      <Field
        label="glossary_terms"
        htmlFor="df-glossary-terms"
        hint="Glossary term URNs, one per line — blank lines dropped, each line edge-trimmed (OR-ed)"
      >
        <Textarea
          id="df-glossary-terms"
          rows={3}
          value={raw.glossary_terms}
          onChange={(e) => setListDimension("glossary_terms", e.target.value)}
          placeholder="urn:li:glossaryTerm:Finance.Revenue"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>

      <Field
        label="dataset_urns"
        htmlFor="df-dataset-urns"
        hint="Explicit dataset URNs, one per line — blank lines dropped, each line edge-trimmed (OR-ed)"
      >
        <Textarea
          id="df-dataset-urns"
          rows={3}
          value={raw.dataset_urns}
          onChange={(e) => setListDimension("dataset_urns", e.target.value)}
          placeholder="urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>
    </fieldset>
  );
}
