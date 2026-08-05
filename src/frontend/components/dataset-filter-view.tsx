/**
 * DatasetFilterView — read-only render of the four dataset_filter dimensions,
 * mirroring DatasetFilterEditor. Empty dimensions show an em dash.
 *
 * dataset_filter shape (mirrors src/api/schemas/_dataset_filter.py):
 *   origin          — optional string
 *   tags            — optional string[] (tag URNs)
 *   glossary_terms  — optional string[] (glossary term URNs)
 *   dataset_urns    — optional string[] (explicit dataset URNs)
 *
 * List entries render monospaced with internal whitespace preserved, so a URN's
 * own spacing reads back as stored (DatasetFilterEditor edge-trims each line on
 * the way in, so leading and trailing whitespace never reaches here).
 */

import { FieldValue } from "@/components/forms/field-value";
import type { DatasetFilter } from "@/types/governance";

function ListValue({ items }: { items: string[] | undefined }) {
  if (!items || items.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <ul className="space-y-0.5 font-mono text-xs">
      {items.map((item, i) => (
        // Duplicate entries are legal (nothing dedupes on the way in), so the
        // index is part of the key.
        <li key={`${i}:${item}`} className="whitespace-pre-wrap break-all">
          {item}
        </li>
      ))}
    </ul>
  );
}

export function DatasetFilterView({ value }: { value: DatasetFilter }) {
  return (
    <fieldset className="space-y-4 rounded-md border p-4">
      <legend className="px-1 text-sm font-medium text-muted-foreground">dataset_filter</legend>

      <FieldValue label="origin">
        {value.origin ? (
          value.origin
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </FieldValue>

      <FieldValue label="tags">
        <ListValue items={value.tags} />
      </FieldValue>

      <FieldValue label="glossary_terms">
        <ListValue items={value.glossary_terms} />
      </FieldValue>

      <FieldValue label="dataset_urns">
        <ListValue items={value.dataset_urns} />
      </FieldValue>
    </fieldset>
  );
}
