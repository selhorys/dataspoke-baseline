/**
 * DatasetFilterView — read-only render of the `dataset_filter` clause, the
 * view-mode analogue of DatasetFilterEditor.
 *
 * `dataset_filter` is a SQL `WHERE`-clause string (spec/API.md
 * §`dataset_filter` grammar). It renders as a monospace `<pre>` block so the
 * stored line breaks and indentation read back exactly as saved; an empty
 * filter (which matches every registered dataset) shows an em dash.
 */

export function DatasetFilterView({ value }: { value: string }) {
  const clause = (value ?? "").trim();

  return (
    <fieldset className="space-y-2 rounded-md border p-4">
      <legend className="px-1 text-sm font-medium text-muted-foreground">dataset_filter</legend>

      {clause.length === 0 ? (
        <span className="text-muted-foreground">—</span>
      ) : (
        <pre className="overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all text-foreground">
          {clause}
        </pre>
      )}
    </fieldset>
  );
}
