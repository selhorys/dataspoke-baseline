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
import { SecretRefAuthoringGuide } from "./secret-ref-authoring-guide";

interface SecretRefHelperProps {
  /** Refs from GET /spoke/ingestion/secrets, or undefined while loading. */
  secrets?: SecretRefInfo[];
  /** True when the secret store returned 503 STORAGE_UNAVAILABLE. */
  unavailable?: boolean;
}

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
      <div className="mt-4">
        <SecretRefAuthoringGuide />
      </div>
    </section>
  );
}
