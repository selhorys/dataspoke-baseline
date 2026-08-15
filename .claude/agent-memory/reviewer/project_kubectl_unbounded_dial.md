---
name: kubectl-unbounded-dial
description: kubectl has no default overall timeout — >120s for one `get` against a blackholed API server; the 4x discovery multiplier makes --request-timeout=3s cost 13s and 10s cost 40s
metadata:
  type: project
---

`kubectl get deployment/... -n ...` against an API server that DROPS packets does not return.
Measured on this machine (kubectl v1.33.2, `server: https://192.0.2.1:6443`): a single `get`
exceeded **120s** with no flag; `--request-timeout=10s` → **40s**; `--request-timeout=3s` → **13s**; `--request-timeout=2s` →
~8s. The overshoot is the discovery call (`/api`) being retried 4x, each retry paying the full
request timeout, before the real GET is even attempted.

**Why:** it matters whenever a script's hang budget is audited. `helm-charts/bin/health-check.sh`
bounds its raw `/dev/tcp` probes at ~6s precisely because a blocking PreToolUse hook must not
stall an integration run — but its `_deployment_state` / `_check_ready_replicas` kubectl reads
(up to ~6 per run) carry no `--request-timeout`, and removing `--quick` put them on that hook's
path for the first time. A reachable-control-plane-but-zero-nodes cluster (GKE Autopilot at
rest, memory `project_gke_autopilot`) hides this: the API server answers instantly, so a
"fully-down cluster" wall-clock measurement proves nothing about the offline/VPN-down case.

**How to apply:** when a report cites a measured worst case for a script that shells out to
kubectl, ask which failure the measurement reproduced. Only an unreachable *endpoint* exercises
the dial path. Suggest `--request-timeout` on every network-touching kubectl call, and quote the
4x discovery multiplier so the chosen value is not mistaken for the wall clock.

**Resolved in health-check.sh (2026-08-15):** every read now carries `--request-timeout=3s`, and
`_deployment_state` memoizes an unreachable server in `_KUBE_UNREACHABLE` so only the FIRST read
pays the 13s. Measured budget for a fully-blackholed cluster: 25s of bounded TCP/HTTP probes plus
one 13s dial = **~38s**, against the preflight hook's own `HC_TIMEOUT=45`. Re-measure that margin
whenever a probe or a non-memoized kubectl read is added.
