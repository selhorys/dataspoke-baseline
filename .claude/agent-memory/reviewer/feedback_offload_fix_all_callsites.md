---
name: offload-fix-all-callsites
description: "Moved the blocking call off the event loop" is a per-call-site claim; enumerate every path and heartbeat-probe each one
metadata:
  type: feedback
---

When a generator reports that a synchronous/blocking dependency call was moved off the
event loop (`anyio.to_thread.run_sync`, `run_in_executor`, …), do not accept the claim
globally. Enumerate every call site of that same primitive and probe each plane
separately.

**Why:** in the rate-limiter batch the report's fix table said "Sync storage call blocks
the loop → `await to_thread.run_sync(limiter_._check_request_limit, …)`" and pasted a
heartbeat measurement proving the loop stayed live. That measurement was taken on the
*middleware* plane only. The second call site — slowapi's route decorator
(`slowapi/extension.py:735`, `async_wrapper` calls `self._check_request_limit(...)`
inline) — was untouched, so the fail-closed auth routes still froze the whole worker for
the full socket timeout. The report's own timing table for that plane showed the stall
(2018 ms) but omitted the heartbeat column, so the gap read as fixed.

**How to apply:** grep the blocking primitive by name across the diff *and* its
third-party callers; then run a probe per plane — a `asyncio.sleep(0.01)` heartbeat task
counting ticks while one request is in flight. Ticks ≈ 0 during a multi-second request
means the loop is frozen. Quantify against the k8s probe budget
(`api-deployment.yaml` readinessProbe `timeoutSeconds`) before assigning severity.

Related: [[verify-branch-reachability-rationales]], [[slowapi-bucket-scope]]
