---
name: health-check-verdict-contract
description: health-check.sh's gate contract after the 2026-08-15/16 passes — the earlier holes are measured CLOSED (incl. _bounded's grandchild orphans); what remains is `exit` inside the sourced env file, an unpinned hook timeout, 2 fail-opens, and three doc surfaces that contradict the code's tool gate
metadata:
  type: project
---

`helm-charts/bin/health-check.sh` is the gate five consumers branch on
(`.claude/hooks/preflight-integration-tests.sh`, the PRauto loop master
(`spec/AI_PRAUTO.md §Dev Cluster and Deploys`),
`.claude/skills/{k8s-deploy/SKILL.md,test-manual-api-wired/SKILL.md,
test-manual-ui/helpers/preflight.sh}`).

**Measured closed (bash 3.2.57, macOS, stubbed kubectl/curl/redis-cli/uv, no
cluster). Re-runnable harness recipe: stub dir on PATH + a throwaway env file
with ports nothing listens on; a python `accept()`-and-never-`recv()` listener
covers the stall case.**

- `--quick` gone repo-wide (only `.claude/agent-memory/` names it).
- **Every pre-probe abort exits 2** — measured: missing tool (kubectl/curl),
  unknown flag, `--profile`/`--env-file` with an empty value, invalid profile,
  missing env file, env-file syntax error, unset `DATASPOKE_KUBE_CLUSTER`,
  `kubectl config use-context` rc≠0, invalid `INGRESS_SCHEME`/`INGRESS_DOMAIN`.
  The last two abort inside `$(ingress_scheme)` / `$(datahub_gms_host)`, where
  `error()`'s exit only ends the substitution subshell — the parent still
  reaches 2 because `set -e` exits with the failed assignment's status. An env
  file setting `DATASPOKE_ERROR_EXIT_CODE=0` cannot rewrite it (re-asserted
  after the source, and `error()` clamps to 1-255).
- **Probe region never leaks 2** — measured with every external tool stubbed to
  `exit 2`: run still ends `exit 1`.
- `set +eu` (not `+e`) around the env-file source: a `TYPO="${UNSET}"` line now
  expands empty and the run completes at exit 1 instead of dying at bash's 1.
- **`_bounded`'s process-group kill works** (re-measured 2026-08-16 with a
  verbatim copy of the function): `set -m` + `kill -TERM -$pid` / `kill -9
  -$pid` leaves **no orphaned grandchild** — the earlier "kills the direct
  child only" residual is CLOSED. Same run showed bash's job-control
  `Terminated: 15` notice prints the **unexpanded** job text (`"$@" > "$out"
  2> /dev/null`), so a probe argument (PGPASSWORD, REDISCLI_AUTH) never
  reaches stderr through it.
- `_tcp_check` sends **zero bytes** and is pure bash — no `timeout(1)`.
  `mktemp -t` really does ignore `$TMPDIR` on macOS (re-measured), which is why
  every temp path is built from `${TMPDIR:-/tmp}` by hand.
- NotFound split works: `deployments.apps … not found` → SKIP;
  `namespaces … not found` → SKIP for `langfuse-01`, **FAIL** for the DataSpoke
  namespace; `Forbidden` → FAIL. `--frontend none` still SKIPs; required
  components always FAIL.
- Hook: 0 → exit 0 + marker; 1 → reinstall table, exit 2; 2 → "local
  configuration fault", exit 2; TERM-ignoring child → deadline fires at
  HC_TIMEOUT+2s, process group killed, private TMPDIR reclaimed. Non-owned or
  symlinked marker rejected + removed. `--keep-lock` passed only behind the
  `DATASPOKE_DEV_LOCK_PREACQUIRED=1` prefix.
- No generated credential reaches stdout anywhere in `helm-charts/bin/` — only
  variable NAMES + env-file paths (`langfuse.sh:96` prints the Langfuse
  *public* key; `install-prod-preflight.sh:762` writes to a temp env file).

**Live residuals — check these first on any later diff:**

1. **`exit` inside the sourced env file bypasses everything.** An env file whose
   first line is `exit 0` makes the script exit **0** with no banner and no
   probes — the hook then writes its 60s bypass marker and the PRauto loop
   master logs "health check passed"; `exit 1` makes the loop master provision
   a GKE cluster. The
   env file is `source`d, so it is already RCE-trusted; this is a
   contract/spec-accuracy gap. `HELM_CHART.md §Health Check` states the exit-2
   rule as admitting no exceptions. Catch-all fix is a region-scoped
   `trap '_rc=$?; (( _setup )) && exit 2' EXIT` — **but order it against
   `use_context`**: that helper chains `prev_trap; rm -f '<pinned kubeconfig>'`,
   and an `exit` inside the earlier trap body pre-empts the chained `rm`, which
   would leak the mode-600 kubeconfig copy.
2. **The hook's 45s budget rides an unpinned harness default.**
   `.claude/settings.json` sets **no `timeout`** for the preflight hook entry,
   so 45+2 only fits inside the runtime's 60s default; a shorter one makes the
   killed hook non-blocking, i.e. fail OPEN. The hook's own comment claims the
   timeout is "configured in .claude/settings.json", which it is not. Pin
   `"timeout": 60` there.
3. **Two deliberate fail-opens remain**: the hook `exit 0`s when
   `health-check.sh` is missing or not executable (and when `jq` is absent the
   command never parses), and `dev_env_healthy` `return 0`s when the script is
   missing.
4. **Worst case can still exceed the hook budget.** Five probes can each stall
   for `PROTOCOL_PROBE_TIMEOUT_SECS=20` (2 Postgres, 2 Kafka, 1 Redis) ≈ 120s vs
   `HC_TIMEOUT=45`, so in the stale-port-forward posture the hook's deadline —
   not the check — renders the verdict. Fail-closed, but no per-service lines.
5. **The tool gate's FOUR doc surfaces contradict the code** (re-measured
   2026-08-16: missing `curl` -> exit 2 naming curl; missing `redis-cli` -> a
   SKIP line and the run can still end "All services healthy", exit 0). Code is
   `require_tools kubectl curl` (:270). Wrong: the `--help` header (:47-54,
   which also says exit 0 means "every probe passed" — skips also exit 0), the
   comment at :250 ("All four tools are gated") which contradicts :259 six lines
   below it, `.claude/skills/k8s-deploy/SKILL.md` (the 2026-08-16 spec pass
   ADDED "requires kubectl, curl, redis-cli and uv ... a missing one aborts with
   exit 2"), and the reviewer's own
   `.claude/agent-memory/reviewer/feedback_setup_fault_vs_verdict_tool_gap.md`
   ("RESOLVED ... require_tools kubectl curl redis-cli uv"), which is where the
   skill text came from and will re-propagate it. `spec/feature/HELM_CHART.md`
   §Tool gate is the ONLY correct surface. `helm-charts/README.md`
   §Prerequisites and its new health-check paragraph both omit `curl`, now a
   hard requirement.
6. **Absence is decided from a hard-coded Deployment name**, not "from the
   release" as `HELM_CHART.md`/`README.md` say: `dataspoke-frontend`,
   `dataspoke-event-consumer`, `langfuse-web`, `langfuse-worker` under release
   `dataspoke`. A prod overlay with `frontend.fullnameOverride` turns a
   deployed-but-broken frontend into `[SKIP] not deployed`. Langfuse absence is
   decided from an env var (`DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE`), a third
   route neither doc describes.
7. **`.claude/.cache/healthcheck-ok.mtime`** (the hook's bypass token) is NOT
   gitignored — `git add -A` commits it, and a fresh checkout's mtime is "now",
   buying up to 60s of ungated pytest runs.

8. **`--keep-lock` rule vs its own callers.** `HELM_CHART.md` §Callers now says
   the rule "binds every automated caller ... pass it only for a lock the run
   already owns", but the same batch made `.claude/skills/k8s-deploy/SKILL.md`
   pass it unconditionally in its deploy pre-flight (documented there as a
   deliberate trade). Three automated callers, two follow the rule.
9. **Spec overstates the Langfuse skip.** §Absence vs failure says that with
   `DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE` unset "both halves skip regardless".
   Measured false in managed mode with the ingress down: `check_dataspoke_langfuse`
   `_fail`s langfuse-web at the `_ingress_port_open` gate before the namespace is
   ever consulted. Shared mode (the prod posture) does skip both. Also, the
   "default `<release>-<component>` names of the `dataspoke` release" wording does
   not describe `langfuse-web`/`langfuse-worker`, which are the langfuse chart's
   own names.

**Still not on the sensitive-path glob list:** `.claude/settings.json` (the
hook's `if` filter, its timeout, the permission allow/deny lists),
`.claude/agent-memory/**` (checked-in evaluator memory, writable by any
generator), `.claude/skills/test-manual-*/**` (one is an executable exit-code
consumer), `spec/AI_PRAUTO.md`. `.claude/hooks/**` was added 2026-08-15.

**How to apply:** on any diff to the script or its doc surfaces
(`HELM_CHART.md §Health Check`, `README.md §Health check`, `TESTING.md
§Prerequisites`, `k8s-deploy/SKILL.md`, `AI_PRAUTO.md §Provisioning`,
`.claude/hooks/**`), enumerate the abort sites and **run them** with the stub
harness above — the recurring defect is prose that lists a condition the code
does not route through `error()`, or a boundedness/tool-gate claim no probe
honours. Docs drift one pass behind the code: check `--help`, README
Prerequisites and the k8s-deploy skill against `require_tools`, not against
each other.
