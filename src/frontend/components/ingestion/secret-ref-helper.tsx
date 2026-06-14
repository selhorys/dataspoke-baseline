"use client";

/**
 * SecretRefHelper — source-credential reference picker + authoring guide.
 *
 * Two read-only parts shown beside the recipe editor for ACTIVE_CUSTOM_MANAGED
 * sources:
 *
 *  1. Available references — the `${name__key}` tokens the cluster already
 *     exposes, fetched via `GET /spoke/ingestion/secrets` (one row per
 *     `(secret, key)` under the `dataspoke-source-cred-` prefix; values are
 *     never returned).
 *  2. A collapsible authoring guide — static instruction for provisioning a new
 *     credential. DataSpoke is reference-only (no secret-write endpoint), so
 *     this is documentation, not a form: it shows the `kubectl create secret`
 *     recipe, the in-cluster namespace note, the `dataspoke-source-cred-` prefix
 *     as a security boundary, and the `${name__key}` reference syntax. It calls
 *     no write route.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Create View / §Components
 * (SecretRefHelper); spec/feature/SECRET_RESOLUTION.md §Admin authoring guide.
 */

import type { SecretRefInfo } from "@/types/ingestion";

interface SecretRefHelperProps {
  /** Refs from GET /spoke/ingestion/secrets, or undefined while loading. */
  secrets?: SecretRefInfo[];
  /** True when the secret store returned 503 STORAGE_UNAVAILABLE. */
  unavailable?: boolean;
}

// `kubectl create secret generic dataspoke-source-cred-<name>
//  --from-literal=<key>=<value> -n <dataspoke-namespace>`
// rendered as static text. The `<dataspoke-namespace>` placeholder is the API
// pod's own namespace; we do not template a concrete value because the
// reference list (GET /secrets) is the only API surface this component reads.
const KUBECTL_RECIPE =
  "kubectl create secret generic dataspoke-source-cred-<name> \\\n" +
  "  --from-literal=<key>=<value> \\\n" +
  "  -n <dataspoke-namespace>";

export function SecretRefHelper({ secrets, unavailable }: SecretRefHelperProps) {
  return (
    <section className="rounded-lg border p-4">
      <h2 className="mb-2 text-sm font-medium">Secret references</h2>

      {/* Available references — sourced from GET /spoke/ingestion/secrets. */}
      {unavailable ? (
        <p className="text-xs text-muted-foreground">
          Secret store is unavailable (503). You can still reference
          <code className="mx-1 font-mono">{"${name__key}"}</code>
          tokens — the server validates them on save.
        </p>
      ) : secrets && secrets.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {secrets.map((s) => (
            <li key={s.ref}>
              <code className="rounded bg-amber-500/20 px-1.5 py-0.5 font-mono text-xs font-semibold text-amber-700 dark:text-amber-400">
                {`\${${s.ref}}`}
              </code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">
          No source-credential references available. Admins pre-create the
          Kubernetes Secrets out-of-band.
        </p>
      )}

      {/* Read-only authoring guide — DataSpoke has no secret-write API. */}
      <details className="group mt-4 border-t pt-3">
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
    </section>
  );
}
