"use client";

/**
 * SecretRefAuthoringGuide — collapsible, read-only guide for provisioning a new
 * source-credential reference. DataSpoke is reference-only (no secret-write API),
 * so this is documentation: the `kubectl create secret` recipe, the in-cluster
 * namespace note, the `dataspoke-source-cred-` prefix security boundary, and the
 * `${name__key}` reference syntax. Reused by the Create page's SecretRefHelper and
 * the source-detail recipe editor. Calls no write route.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Create View / §Source Detail §Recipe;
 * spec/feature/SECRET_RESOLUTION.md §Admin authoring guide.
 */

// `kubectl create secret generic dataspoke-source-cred-<name>
//  --from-literal=<key>=<value> -n <dataspoke-namespace>`
// rendered as static text. The `<dataspoke-namespace>` placeholder is the API
// pod's own namespace; we do not template a concrete value because the
// reference list (GET /secrets) is the only API surface this component reads.
const KUBECTL_RECIPE =
  "kubectl create secret generic dataspoke-source-cred-<name> \\\n" +
  "  --from-literal=<key>=<value> \\\n" +
  "  -n <dataspoke-namespace>";

export function SecretRefAuthoringGuide() {
  return (
    <details className="group border-t pt-3">
      <summary className="cursor-pointer list-none text-xs font-medium text-muted-foreground hover:text-foreground">
        <span className="group-open:hidden">▸ </span>
        <span className="hidden group-open:inline">▾ </span>
        How to author a new source-credential reference
      </summary>

      <div className="mt-3 space-y-3 text-xs text-muted-foreground">
        <p>
          DataSpoke is reference-only — there is no secret-write API. An admin
          provisions the credential out-of-band, then a recipe references it.
          Create the backing Kubernetes Secret:
        </p>

        <pre className="overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre text-foreground">
          {KUBECTL_RECIPE}
        </pre>

        <ul className="list-disc space-y-1.5 pl-4">
          <li>
            <span className="font-medium text-foreground">Namespace.</span>{" "}
            <code className="font-mono">{"<dataspoke-namespace>"}</code> is the
            API pod&apos;s own (in-cluster) namespace — the Secret must live
            alongside the API.
          </li>
          <li>
            <span className="font-medium text-foreground">Name prefix.</span>{" "}
            The Secret name must start with{" "}
            <code className="rounded bg-muted px-1 font-mono">
              dataspoke-source-cred-
            </code>
            . This prefix is the security boundary — only Secrets under it are
            resolvable from recipes.
          </li>
          <li>
            <span className="font-medium text-foreground">Reference it.</span>{" "}
            In the recipe, reference the new key as{" "}
            <code className="rounded bg-amber-500/20 px-1 font-mono font-semibold text-amber-700 dark:text-amber-400">
              {"${name__key}"}
            </code>{" "}
            — the <code className="font-mono">name</code> is the Secret name
            without the <code className="font-mono">dataspoke-source-cred-</code>{" "}
            prefix, and <code className="font-mono">key</code> is the data key.
          </li>
        </ul>

        <p>
          After creating the Secret, it appears in the reference list above on
          the next refresh of <code className="font-mono">GET /spoke/ingestion/secrets</code>.
        </p>
      </div>
    </details>
  );
}
