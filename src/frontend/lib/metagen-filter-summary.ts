/**
 * summarizeDatasetFilter — render a compact one-line summary of a
 * `dataset_filter` for list views.
 *
 * The filter is a SQL `WHERE`-clause string (API §`dataset_filter` grammar); an
 * empty clause means "all datasets". Line breaks and runs of whitespace collapse
 * to single spaces so a multi-line clause fits one table cell, and a long clause
 * is truncated with an ellipsis — the full text lives on the conf detail page.
 */

/** Maximum characters rendered in a list cell before truncation. */
export const FILTER_SUMMARY_MAX_CHARS = 60;

export function summarizeDatasetFilter(
  filter: string | null | undefined,
  maxChars: number = FILTER_SUMMARY_MAX_CHARS,
): string {
  const collapsed = (filter ?? "").replace(/\s+/g, " ").trim();
  if (collapsed.length === 0) return "all datasets";
  if (collapsed.length <= maxChars) return collapsed;
  return `${collapsed.slice(0, maxChars).trimEnd()}…`;
}
