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
 * Each multi-value field is rendered as a textarea (newline or comma-separated).
 */

import { useCallback } from "react";
import { Field } from "@/components/forms/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { DatasetFilter } from "@/types/governance";

export interface DatasetFilterEditorProps {
  value: DatasetFilter;
  onChange: (v: DatasetFilter) => void;
  disabled?: boolean;
}

export function splitList(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function joinList(arr: string[] | undefined): string {
  return (arr ?? []).join("\n");
}

export function DatasetFilterEditor({ value, onChange, disabled = false }: DatasetFilterEditorProps) {
  const set = useCallback(
    (patch: Partial<DatasetFilter>) => onChange({ ...value, ...patch }),
    [value, onChange],
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
        hint="Tag URNs, one per line or comma-separated (OR-ed)"
      >
        <Textarea
          id="df-tags"
          rows={3}
          value={joinList(value.tags)}
          onChange={(e) => {
            const list = splitList(e.target.value);
            set({ tags: list.length ? list : undefined });
          }}
          placeholder="urn:li:tag:env:DEV"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>

      <Field
        label="glossary_terms"
        htmlFor="df-glossary-terms"
        hint="Glossary term URNs, one per line or comma-separated (OR-ed)"
      >
        <Textarea
          id="df-glossary-terms"
          rows={3}
          value={joinList(value.glossary_terms)}
          onChange={(e) => {
            const list = splitList(e.target.value);
            set({ glossary_terms: list.length ? list : undefined });
          }}
          placeholder="urn:li:glossaryTerm:Finance.Revenue"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>

      <Field
        label="dataset_urns"
        htmlFor="df-dataset-urns"
        hint="Explicit dataset URNs, one per line or comma-separated (OR-ed)"
      >
        <Textarea
          id="df-dataset-urns"
          rows={3}
          value={joinList(value.dataset_urns)}
          onChange={(e) => {
            const list = splitList(e.target.value);
            set({ dataset_urns: list.length ? list : undefined });
          }}
          placeholder="urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
          disabled={disabled}
          className="font-mono text-xs"
        />
      </Field>
    </fieldset>
  );
}
