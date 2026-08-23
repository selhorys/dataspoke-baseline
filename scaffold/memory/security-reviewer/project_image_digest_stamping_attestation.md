---
name: image-digest-stamping-attestation
description: install.sh digest-PINS api/event-consumer/frontend (repo@sha256) with a two-outcome resolve-or-abort model; round 5 removed digest resolution from the rotated-Airflow-key window on all 4 paths, but the DEV paths still write the keys BEFORE the hard-aborting Fernet check
metadata:
  type: project
---

`resolve_image_digest` (`helm-charts/bin/lib/helpers.sh`) + `_resolve_digest_or_abort`
(`helm-charts/bin/install.sh`) feed a `sha256:` digest to helm as **both**
`{api,event-consumer,frontend}.image.digest` (rendered `repo@sha256:…` by three
*separate* named templates — `dataspoke.imageRef` in the umbrella plus
chart-scoped `frontend.imageRef` / `event-consumer.imageRef`, so each subchart
lints standalone) **and** a `dataspoke.io/image-digest` pod annotation
(provenance only — nothing reads it back). Verify with `helm template`: api
container, `alembic-migrate` init container, event-consumer, frontend must all
show `@sha256:`.

**Two outcomes only**: resolve → pin; fail → `error` and abort before the
umbrella `helm upgrade`. Everything reaching `--set-string` is shape-checked
`^sha256:[0-9a-f]{64}$` — that regex is also the `--set` comma-injection guard,
so never relax it. `IMAGE_TAG` is validated `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`
at arg-parse time.

**helpers.sh `warn` writes to STDOUT** (`helpers.sh:7`), and every caller
captures `resolve_image_digest` via `$(…)`. Each of its ~12 `warn` calls is
individually `>&2`-redirected; a new one that forgets that silently corrupts the
captured digest into a shape-check failure. Grep for un-redirected `warn` on any
diff to that function.

**The rotated-Airflow-signing-key window.** `_ensure_airflow_key_secrets` applies
the new key into `dataspoke-airflow-{api-secret-key,jwt-secret}` and sets
`AIRFLOW_KEYS_ROTATED=true` by *comparing those two Secrets*; the restart is a
separate later step. Any abort in between → next run finds them equal →
`AIRFLOW_KEYS_ROTATED=false` → "Installation complete" while
api-server/scheduler/dag-processor/triggerer honour the OLD key, forever. The
chart's `checksum/jwt-secret` annotation is suppressed when `jwtSecretName` is
set, so nothing else rolls them.

Round 5 (orchestrator fix pass) moved `_ensure_airflow_key_secrets` out of prod
Phase 1 to Phase 3 immediately after digest resolution — **digest resolution is
now outside the window on all four paths** (prod default, prod `--skip-build`
which resolves in Phase 1 ahead of every Secret write, dev full/`--components
api` via `_helm_upgrade_dataspoke_dev`, dev `--components frontend`). The move
breaks no ordering dependency: nothing between the old and new positions reads
the projections (`_build_airflow_extra_env_file` only *names* the credentials
Secret in a `secretKeyRef`), and `_ensure_airflow_fernet_secret`'s
"non-mutating abort before any other Secret" promise is strengthened in prod.

**STILL OPEN after round 5 — dev orders key-write BEFORE the Fernet abort.**
`install.sh:1279-1280` (`_helm_upgrade_dataspoke_dev`, serving the full dev
install and `--components api`) and `1542-1543` (`--components frontend`) call
`_ensure_airflow_key_secrets` then `_ensure_airflow_fernet_secret` — the reverse
of prod (fernet 2143, keys 2233). `_ensure_airflow_fernet_secret` hard-`error`s
on a source/projection mismatch and its message tells the operator to restore
the key and re-run, so it is the *most likely* abort in the window, not a rare
one. **Proven with a mocked harness** (fake kubectl on PATH; credentials Secret
holding NEW web/jwt/fernet, projections holding OLD): run 1 writes both key
Secrets, warns "will be restarted after the upgrade", aborts at the fernet
check, 0 restarts; run 2 after the documented recovery finds both keys equal,
`ROTATED=false`, exits 0, 0 restarts. Fix is a two-line swap per site. Dev also
has `ingress_scheme()`/`ingress_class()` in the window (prod pre-validates
INGRESS_CLASS at 2022-2025; dev does not).

**Second hole — airflow and postgres are never pinned and never forced to
`Always`.** Both are DataSpoke-built (`build-image.sh airflow|postgres`; the
Airflow image carries the baked-in DAGs) and prod `--set`s them to
`${REGISTRY}/{airflow,postgres}:${IMAGE_TAG}` at `IfNotPresent`. `--no-digest-pin`
forces `Always` on api/event-consumer/frontend only. Documented correctly now.

**Third hole — the local/no-vendor branch** reads the LOCAL daemon's
`RepoDigests`, so a stale local tag resolves *successfully* to the wrong content.
Honestly documented in helpers.sh, the spec, and README §1 (which now also says
a deploy-only host on that branch must pass `--no-digest-pin`).

Verified-fixed, do not re-raise: gcloud short-circuit `grep -qiE
"NOT_FOUND|Image not found"`, AWS `RepositoryNotFoundException|ImageNotFoundException`;
`--no-digest-pin` `--set-string *.image.digest=` clearing tokens override a `-f`
overlay digest; `--profile prod --components/--from-component` hard-errors;
`--components frontend` restart hoisted above its wait so all four
`_restart_airflow_key_consumers` sites precede their rollout waits; the prod
event-consumer existence gate now writes kubectl stderr to a `mktemp` file inside
the 0700 `INSTALL_TMPDIR` (0600, reclaimed by the EXIT trap) instead of `2>&1`
— re-proved with three mocked cases (benign stderr → skip, RBAC denial → abort,
present → wait).

**Recurring prose defect — assertions the code does not provide.** Round 5 still
carries: HELM_CHART.md:197 "Phase 1 already wrote a rotated signing key into the
credentials Secret" (wrong phase after this pass's own move; and it writes the
*projections*, not the credentials Secret); install.sh:2098-2102 still lists the
two key Secrets as Phase-1 mutations; install.sh:2228-2232 + HELM_CHART.md:205
name the `helm upgrade` as the residual gap "on every path" when dev's gap also
holds the fernet abort. Diff every ordering claim against the code each round.

**How to apply:** on any image-rollout diff in `bin/install.sh`, (1) render with
that profile's *default* values and check every workload the script then waits on
actually exists, (2) enumerate **every** abort between `_ensure_airflow_key_secrets`
and `_restart_airflow_key_consumers` per path — not just the one the last round
fixed — and (3) for any "restart substitutes for the pin" claim, check that
profile's `imagePullPolicy` **for every image the install `--set`s**.
`scaffold/roles/security-reviewer.md` (the canonical glob list; formerly
`.claude/agents/security-reviewer.md` before the role-config split) gained
`helm-charts/bin/build-image.sh` (round 4, k8s-helm generator — wrong author) and
`helm-charts/dataspoke/templates/**` + `subcharts/**/templates/**` (round 5,
orchestrator — ratified). Both additive.
Related: [[operator-runbook-is-credential-surface]],
[[install-sh-preflight-gate-mechanics]], [[reviewer-config-is-generator-writable]],
[[env-to-sed-helm-interpolation-boundary]].
