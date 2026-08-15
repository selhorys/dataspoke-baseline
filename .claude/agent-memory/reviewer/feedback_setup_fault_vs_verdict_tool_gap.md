---
name: health-check-exit2-tool-gap
description: RESOLVED in code (require_tools kubectl curl redis-cli uv) — but a widened tool gate is a new OPERATOR PREREQUISITE, and helm-charts/README.md §Prerequisites still lists only kubectl/helm/python3
metadata:
  type: feedback
---

When a script splits its exit codes into "could not be set up" vs "probes ran and something is
unhealthy", do not stop at auditing the abort sites (that is
[[exit-code-contract-set-e-holes]]). Also enumerate every **external binary the probe region
shells out to** and ask what the script reports when that binary is missing.

**Why:** `helm-charts/bin/health-check.sh` gained `require_tools kubectl` and a full exit-2
pre-probe contract, and every abort path was verified at 2. But the probe region also needs
`curl` (all HTTP verdicts), `redis-cli` (the only Redis probe) and `uv` (Kafka, and the Postgres
fallback), and none of them is in `require_tools`. Measured on 2026-08-15 with a `curl`/`redis-cli`
shim that exits 127: the script prints **11 `[FAIL]` lines and exits 1** — indistinguishable from a
dead cluster. `.prauto`'s `dev_env_healthy` answers exit 1 by calling `provision_dev_env`, i.e. an
unsupervised `install.sh --profile dev`. A missing local binary therefore triggers the exact
consequence the 1-vs-2 split was introduced to prevent.

A tell that the gap is real: the resulting lines are *individually* plausible
(`/health did not return 2xx`, `PING did not return PONG`), so nothing in the report hints at a
tooling gap. `_http_alive`'s `[[ "$code" != "000" ]]` even mislabels the empty output of a missing
curl as "HTTP alive but health endpoint reports unhealthy".

**How to apply:** grep the probe region for bare command names, subtract `require_tools`'s list,
and report the difference. Distinguish hard dependencies (no fallback → belongs in
`require_tools`) from ones with a real fallback (`pg_isready` falls back to `uv run python`).
The same question applies to any gate whose exit code a consumer treats as evidence about a
remote system.

**Resolved 2026-08-15**: `require_tools kubectl curl redis-cli uv` now runs in the pre-probe
region, so a missing binary is an exit 2 naming it. The follow-on lesson: a widened tool gate
turns a degraded verdict into a HARD ABORT, so the new binaries are operator prerequisites and
must be propagated to the operator-facing doc. `helm-charts/README.md` §Prerequisites still
lists only kubectl / helm / python3 while `spec/feature/HELM_CHART.md` and
`.claude/skills/k8s-deploy/SKILL.md` list all four — and on `--profile prod` a missing
`redis-cli` now yields NO verdict at all, not even the `DataSpoke Infra` section that same
README tells a prod operator to read. Check the prerequisite list whenever a gate widens.
