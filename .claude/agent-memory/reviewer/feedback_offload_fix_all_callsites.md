---
name: offload-fix-all-callsites
description: "Fixed X" is a per-call-site claim — enumerate every call site of the same primitive and probe each plane; applies to off-loop moves AND to ordering invariants asserted across sibling call sites
metadata:
  type: feedback
---

When a generator reports that a shared primitive was fixed (moved off the event loop,
reordered, gated), do not accept the claim globally. Enumerate every call site of that
same primitive and probe each one separately.

**Why (async):** in the rate-limiter batch the report's fix table said "Sync storage call
blocks the loop → `await to_thread.run_sync(limiter_._check_request_limit, …)`" and
pasted a heartbeat measurement proving the loop stayed live. That measurement was taken
on the *middleware* plane only. The second call site — slowapi's route decorator
(`slowapi/extension.py:735`, `async_wrapper` calls `self._check_request_limit(...)`
inline) — was untouched, so the fail-closed auth routes still froze the whole worker for
the full socket timeout.

**Why (shell ordering):** `helm-charts/bin/install.sh` has four call sites of
`_restart_airflow_key_consumers`. A round-4 fix hoisted the **prod** one above its
`rollout status … || error` waits (correct — an abort otherwise strands a rotated signing
key that the next run's Secret-vs-projection comparison then reports as already in sync),
and shipped a code comment plus a `spec/feature/HELM_CHART.md` paragraph asserting "the
other three call sites already order it this way". The dev `--components frontend` fast
path does not: `_ensure_airflow_key_secrets` → `rollout status deployment/dataspoke-frontend
… || error` → `_restart_airflow_key_consumers`. `git show HEAD:` confirmed it predates the
round, so the *bug* is old but the *assertion* is new.

**How to apply:** grep the helper/primitive by name across the whole file (and its
third-party callers), list the line numbers, and check the surrounding order at each —
`grep -n "_helper_name\|rollout status\|_ensure_" file` renders the interleaving in one
screen. For async planes, run a `asyncio.sleep(0.01)` heartbeat probe per plane. Treat any
"every other call site already does this" sentence in a diff as a claim to verify, not
context. Related: [[verify-branch-reachability-rationales]], [[slowapi-bucket-scope]],
[[guard-annotation-all-render-paths]]
