---
name: optin-workload-unconditional-wait
description: RESOLVED for event-consumer (existence gate + separated stderr) — but `api.enabled` is the SAME shape and its wait is still ungated; keep the rule that every post-deploy `rollout status || error` needs a rendered-by-default check
metadata:
  type: feedback
---

Before accepting any unconditional post-deploy wait (`kubectl rollout status
deployment/X -n NS || error`), check whether `X` is actually rendered by the
values in force for that profile. Read the subchart's `condition:` in
`Chart.yaml` **and** the template's own `{{- if .Values.<x>.enabled }}` guard —
not the dev overlay you happen to be testing on. Then check whether
`install.sh` `--set`s that `enabled` key itself: if it does, the script's own
flag is authoritative and an overlay cannot turn the workload off; if it does
not, an operator overlay wins and the wait is reachable-by-abort.

**Why (event-consumer — fixed):** `helm-charts/dataspoke/Chart.yaml` gates the
event-consumer subchart on `event-consumer.enabled`, `false` in `values.yaml`
(prod) and `true` only in `values-dev.yaml`. An unconditional prod
`rollout status deployment/dataspoke-event-consumer || error` fired on every
default prod install. Fixed: install.sh now gates on
`kubectl get deployment/... --ignore-not-found -o name`, with stderr redirected
to a file in `INSTALL_TMPDIR` instead of `2>&1` (a merged benign kubectl notice
on an exit-0 NotFound made the gate read "deployed"). Verified 2026-08-01 with a
mocked kubectl in three modes — NotFound+stderr-warning ⇒ skip, found ⇒ wait,
RBAC denial ⇒ error.

**Second instance, still open — `api`.** `dataspoke/templates/api-deployment.yaml`
line 1 is `{{- if .Values.api.enabled }}` (default `true` in `values.yaml`), and
the prod branch `--set`s `frontend.enabled` and gates its frontend wait on it,
but never `--set`s `api.enabled` — so an operator overlay setting it false wins
and `kubectl rollout status deployment/dataspoke-api ... || error` aborts the
install. `spec/feature/HELM_CHART.md`'s post-upgrade-waits table asserts the
opposite ("the api Deployment is unconditional in every prod install").
`api.enabled` is absent from `values-prod.example.yaml`, which caps likelihood —
not the false claim.

**How to apply:** the asymmetry is the tell — `_rollout_restart_workload`
`kubectl get`s first and prints "not found — skipping", and a sibling wait in the
same block usually IS gated, so one ungated wait next to two gated ones is the
bug. Prove it cheaply: `helm template <chart> -n ns` with **profile defaults
only** plus `--set <x>.enabled=false`, `grep -c <workload-name>`. Related:
[[guard-annotation-all-render-paths]], [[verify-branch-reachability-rationales]].
