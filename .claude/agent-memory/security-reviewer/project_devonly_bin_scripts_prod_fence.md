---
name: devonly-bin-scripts-prod-fence
description: The dev-only helm-charts/bin scripts (port-forward.sh, health-check.sh) are fenced off from prod only by `set -u` on DATASPOKE_DEV_* vars — any `:-` default silently unlocks them for prod, onto fixed ports the test suite defaults to
metadata:
  type: project
---

`helm-charts/bin/port-forward.sh` and `bin/health-check.sh` have **no profile gate of
their own**. What kept them off a prod deployment was accidental: `set -euo pipefail`
plus an unquoted `${DATASPOKE_DEV_KUBE_*_NAMESPACE}` reference. `.env.prod.example`
carries no `DATASPOKE_DEV_*` line by design (its own header: "a line here makes this
file ambiguous"), so those references were an "unbound variable" abort on every
conforming prod env file. Adding `:-` to one of them is therefore not a robustness fix —
it removes the fence.

**Why it matters (measured 2026-08-15, issue #153/#156 diff):** with `DH_NS`/`DD_NS`
defaulted to empty, `port-forward.sh --env-file .env.prod` succeeds and opens
`127.0.0.1:9201 -> <prod-ns>/dataspoke-postgresql` and `:9202 -> dataspoke-redis-master`.
It pins the right cluster (`use_context "${DATASPOKE_KUBE_CLUSTER}"` at :40), so those
really are prod backends. Those ports are hardcoded as "the canonical `DATASPOKE_DEV_*`
ports", and `tests/integration/util/__main__.py:118-122` defaults to
`localhost:9201 / dataspoke / dataspoke` **with no env exported at all** — its
`--reset-all` truncates. The only thing between a routine reset and a prod truncate is
that the dev and prod Postgres passwords differ; nothing enforces or checks that.
`port-forward.sh:104` then prints `set -a && source ${ENV_FILE} && ... pytest
tests/integration/api_wired/` with the *prod* file interpolated.

Three facts to re-check on any `bin/` diff of this shape:

1. **`ENV_FILE` is exported** (`install.sh:177`), and both scripts fall back to it before
   `.env.dev`. So "the operator must type `--env-file .env.prod`" is false — a shell that
   ran a prod install, or follows the `ENV_FILE=` convention, selects prod with no flag.
2. **`health-check.sh` never calls `use_context`** (`port-forward.sh` does). Its two
   `kubectl` checks (event-consumer, langfuse-worker) read the operator's *ambient*
   context while the banner names the env file — so a `--profile prod` run can PASS/skip
   a component read off the dev cluster. README claims the opposite.
3. **`--quick` in `shared` mode is nearly vacuous**: `_ingress_port_open` returns 0
   unconditionally, so api / airflow / frontend / langfuse-web / datahub-gms all
   `_pass "(tcp)"` without a single packet. Prod defaults to `shared`, and the runbook
   says "prefer `--quick` on prod" + "read the DataSpoke Infra section and nothing else".

**How to apply:** treat any `${DATASPOKE_DEV_*:-}` added to a `bin/` script as a
prod-unlock and demand an explicit gate in the same diff. `lib/helpers.sh` already has
`seed_profile <env_file>` (:624) — the shared profile decision — so the fix is a banner
naming env file + cluster + namespace *before* the first mutating/forwarding call, plus
an `--allow-prod`-style acknowledgement. Same class as
[[seed-profile-selection-split]] (per-script profile guesses) and
[[operator-runbook-is-credential-surface]] (the runbook then documents the unlock as a
feature).
