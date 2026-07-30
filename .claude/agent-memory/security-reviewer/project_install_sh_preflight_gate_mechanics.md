---
name: install-sh-preflight-gate-mechanics
description: install.sh prod pre-flight gates only fail closed when the helper runs via command substitution in an assignment; the "-" sentinel split and --values existence check are now correct, but the StorageClass gate can pass on a value the chart never uses (subchart-scoped global.storageClass) and wrong-typed overlay nodes fail open
metadata:
  type: project
---

`helm-charts/bin/lib/helpers.sh:8` — `error() { echo …; exit 1; }`. Under
`install.sh`'s `set -euo pipefail` that only aborts the **current shell**, so
the invocation shape decides whether a pre-flight gate is a gate:

- **Fails closed** — `VAR="$(_helper "$f")"` (optionally `|| error …`). The
  assignment inherits the substitution's status and `set -e` aborts.
  (`_resolve_existing_secret_name`, `_resolve_storage_classes`; the latter is
  consumed with `done <<< "$VAR"`, a herestring, so `error` inside the loop
  exits the parent.)
- **Fails OPEN** — `while … done < <(_helper "$f")`. A process substitution's
  status is never examined. `_api_image_helm_set_args` is still this shape
  (benign — emits `--set` flags, not a gate). Reproduce by asserting the
  **parent's** exit status.

**CLOSED, verified — do not re-litigate.** `--values` validates
`[[ -f … ]]` at argument-parse time and rejects a repeat. The StorageClass
gate resolves ELEVEN keys and tags each with a provenance so the `-` sentinel
is honoured only where the template maps it: `bitnami` (3 persistence keys +
2 top-level `global.*`) and `airflow-sentinel`
(`airflow.{logs,dags}.persistence.storageClassName`) skip the lookup;
`airflow-literal` (`triggerer`, `workers`, `workers.celery`, `redis`) hard-
errors, because those templates do `storageClassName: {{ tpl … | quote }}`
with no `-` branch. De-dup is on the `(tag, name)` pair. Both StorageClass
names and `secrets.existingSecret` are DNS-subdomain-checked before reaching
`kubectl` argv / a `--set` token, and the four Secret-name flags use
`--set-string`.

**Two live holes.**

1. **The gate can validate a value the chart never uses.** Bitnami's
   `common.storage.class` precedence is `(.global).storageClass |default
   .persistence.storageClass |default (.global).defaultStorageClass`, and a
   **subchart-scoped** `global` block reaches the child as `.Values.global`.
   `postgresql.global.storageClass: bogus` + `postgresql.primary.persistence.
   storageClass: real-class` makes the gate print "StorageClass 'real-class'
   is present" and pass, while `helm template` renders
   `storageClassName: bogus`. Same for `redis.global.storageClass`. The
   resolver reads only **top-level** `global.*`. Also unresolved but honoured:
   `postgresql.readReplicas.persistence.storageClass` (verified renders),
   `postgresql.backup.cronjob.storage.storageClass`,
   `redis.sentinel.persistence.storageClass`.
2. **Wrong-typed overlay nodes fail open.** Both parsers now
   `try/except yaml.YAMLError` and guard the top level, but nested nodes go
   through `as_map(node)` → `{}`. `secrets:` as a list/scalar therefore
   resolves to `""` → `SECRET_TO_CHECK` falls back to `dataspoke-secrets`,
   and Phase 1 then `kubectl apply`s `dataspoke-airflow-metadata-db` and the
   two Airflow key Secrets **derived from that Secret**. Helm does reject the
   shape in Phase 3 (`type mismatch on postgresql`, or `failed parsing
   --set-string data: interface conversion`) — but only after those
   mutations. The pre-`as_map` code raised AttributeError and aborted before
   any Secret was touched.

**`kubectl get <res> "$name"` is not a grammar check.** `kubectl get
ingressclass "-A"` parses `-A` as `--all-namespaces` and lists everything —
exit 0. (Control: `-Zq` → "unknown shorthand flag".) So the IngressClass
probe, whose value `ingress_class()` derives from
`DATASPOKE_KUBE_INGRESS_CLASS` and which lands in three `--set` (not
`--set-string`) tokens, still has the flag-shaped hole the StorageClass and
Secret paths just closed.

**Why:** `spec/feature/HELM_CHART.md` §Prod operator workflow,
`helm-charts/README.md` and `helm-charts/prod-prereq/README.md` all advertise
the prod pre-flight as a hard gate that "fails before any resources are
created" over IngressClass, StorageClass, the credentials Secret, the
thirteen required keys and the Fernet-key shape. None of the above is visible
to `bash -n`, `helm lint`, or `helm template`.

**How to apply:** on any diff adding or moving a prod pre-flight check, judge
the *invocation shape*, the *phase* (anything before Phase 3 runs ahead of
helm's own type check but after Secret derivation), whether a sentinel is
honoured by *every* subchart it is resolved from, and — for anything the
chart resolves through a precedence chain — whether the gate reads the key
that actually **wins**. Reproduce a gate bypass by rendering the same overlay
with `helm template` and diffing what the gate printed against what rendered.

`helm-charts/bin/install.sh` and `helm-charts/prod-prereq/**` are on the
security-reviewer sensitive-path list; `helm-charts/bin/uninstall.sh` (which
conditionally deletes `dataspoke-secrets` and the Airflow fernet-key Secret)
is **not** — worth proposing.

Related: [[env-to-sed-helm-interpolation-boundary]],
[[operator-runbook-is-credential-surface]],
[[credentials-secret-contract-key-addition]],
[[reviewer-config-is-generator-writable]]
