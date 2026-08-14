/**
 * DatasetFilterGuide — folded, read-only grammar reference sitting beneath the
 * DatasetFilterEditor box. Documentation only: it calls no route and validates
 * nothing (the backend owns the grammar).
 *
 * Reuses the collapsible `<details>` pattern of SecretRefAuthoringGuide.
 *
 * Spec: spec/API.md §`dataset_filter` grammar,
 *       spec/feature/FRONTEND_BASIC.md §Shared component notes.
 */

// Rendered verbatim, mirroring spec/API.md §`dataset_filter` grammar.
const GRAMMAR =
  "filter      := ε | expr                        -- empty string = all registered datasets\n" +
  "expr        := term { (AND|OR) term }           -- one operator kind per level\n" +
  "term        := predicate | '(' expr ')'         -- parens nest at most 2 deep\n" +
  "predicate   := scalar_col '=' string\n" +
  "             | scalar_col IN '(' string {',' string} ')'\n" +
  "             | string IN array_col\n" +
  "             | bool_col '=' bool\n" +
  "scalar_col  := dataset_urn | origin | platform_urn\n" +
  "array_col   := tag_urns | glossary_term_urns\n" +
  "bool_col    := is_primary\n" +
  "bool        := TRUE | FALSE                     -- bare word, never quoted\n" +
  "string      := '...'                            -- single quotes only; '' escapes a quote";

const EXAMPLE =
  "origin = 'PROD'\n" +
  "AND is_primary = true\n" +
  "AND (\n" +
  "    'urn:li:tag:area:catalog' IN tag_urns\n" +
  "    OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns\n" +
  ")";

const COLUMNS: ReadonlyArray<{ name: string; kind: string; value: string }> = [
  { name: "dataset_urn", kind: "scalar", value: "The full urn:li:dataset:(…) URN" },
  {
    name: "origin",
    kind: "scalar",
    value: "The URN's third segment — a DataHub FabricType (PROD / DEV / CORP / EI / STG / …)",
  },
  {
    name: "platform_urn",
    kind: "scalar",
    value: "The URN's first segment — urn:li:dataPlatform:…",
  },
  { name: "tag_urns", kind: "array", value: "DataHub tag URNs carried by the dataset" },
  {
    name: "glossary_term_urns",
    kind: "array",
    value: "DataHub glossary-term URNs carried by the dataset",
  },
  {
    name: "is_primary",
    kind: "bool",
    value:
      "true when the dataset is the primary member of its DataHub sibling set, or has no sibling",
  },
];

export function DatasetFilterGuide() {
  return (
    <details className="group border-t pt-3">
      <summary className="cursor-pointer list-none text-xs font-medium text-muted-foreground hover:text-foreground">
        <span className="group-open:hidden">▸ </span>
        <span className="hidden group-open:inline">▾ </span>
        Filter grammar
      </summary>

      <div className="mt-3 space-y-3 text-xs text-muted-foreground">
        <p>
          The filter is a SQL <code className="font-mono">WHERE</code> clause over the
          dataset registry. An empty filter matches every registered dataset.
        </p>

        <pre className="overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre text-foreground">
          {GRAMMAR}
        </pre>

        <ul className="list-disc space-y-1.5 pl-4">
          {COLUMNS.map((column) => (
            <li key={column.name}>
              <code className="rounded bg-muted px-1 font-mono text-foreground">
                {column.name}
              </code>{" "}
              <span className="italic">({column.kind})</span> — {column.value}
            </li>
          ))}
        </ul>

        <p>
          Keywords (<code className="font-mono">AND</code>, <code className="font-mono">OR</code>,{" "}
          <code className="font-mono">IN</code>) and column names are case-insensitive; string
          values are case-sensitive. <code className="font-mono">TRUE</code> /{" "}
          <code className="font-mono">FALSE</code> are case-insensitive bare words — quoting one
          (<code className="font-mono">is_primary = &apos;true&apos;</code>) is a syntax error.
          Mixing <code className="font-mono">AND</code> and{" "}
          <code className="font-mono">OR</code> at one level requires parentheses. Filter text is
          capped at 8,000 characters and 1,000 string literals.
        </p>

        <pre className="overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre text-foreground">
          {EXAMPLE}
        </pre>
      </div>
    </details>
  );
}
