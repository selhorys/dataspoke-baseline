/**
 * summarizeDatasetFilter — render a compact one-line summary of a four-dimension
 * dataset_filter for list views.
 *
 * The filter shape is the standard {origin?, tags?[], glossary_terms?[],
 * dataset_urns?[]} (API §Metric dataset_filter). An empty filter means "all
 * datasets".
 */

import type { DatasetFilter } from "@/types/governance";

export function summarizeDatasetFilter(
  filter: DatasetFilter | Record<string, unknown> | null | undefined,
): string {
  if (!filter) return "all datasets";

  const f = filter as DatasetFilter;
  const parts: string[] = [];

  if (typeof f.origin === "string" && f.origin.trim().length > 0) {
    parts.push(`origin=${f.origin}`);
  }
  if (Array.isArray(f.tags) && f.tags.length > 0) {
    parts.push(`${f.tags.length} tag${f.tags.length === 1 ? "" : "s"}`);
  }
  if (Array.isArray(f.glossary_terms) && f.glossary_terms.length > 0) {
    parts.push(
      `${f.glossary_terms.length} term${f.glossary_terms.length === 1 ? "" : "s"}`,
    );
  }
  if (Array.isArray(f.dataset_urns) && f.dataset_urns.length > 0) {
    parts.push(
      `${f.dataset_urns.length} URN${f.dataset_urns.length === 1 ? "" : "s"}`,
    );
  }

  return parts.length === 0 ? "all datasets" : parts.join(", ");
}
