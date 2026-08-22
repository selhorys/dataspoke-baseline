---
name: untimed-to-timed-client-swap
description: Replacing an untimed curl with urllib/httpx introduces a client timeout that must be compared against the slowest downstream the endpoint fans out to — helpers.sh's 10s cap sits under AirflowClient's 60s
metadata:
  type: feedback
---

When a change swaps `curl` (no `--max-time` = effectively untimed) for a
programmatic client, the new `timeout=` is a **new** behavioral cap. Do not
review it as "reasonable for an HTTP call" — trace what the endpoint does
server-side and compare against the slowest downstream hop.

**Why:** `helm-charts/bin/lib/helpers.sh`'s `api_internal_request` sets
`urlopen(..., timeout=10)`. Four of its five call sites are DB-only
(`/internal/admin/{bootstrap,conf,peripherals/*}` — `get_peripheral_health` is a
plain `select`, and the seed payloads carry no `token`, so no K8s Secret write).
The fifth, `install.sh`'s `--components api` DAG-verify, calls
`/internal/admin/dags/verify` → `AirflowClient.list_dags()`, whose httpx client
is `timeout=60.0` (`src/workflows/airflow/client.py:60`) and which logs in
before the call. A slow-but-working Airflow — exactly the state right after the
umbrella upgrade that precedes this check — trips the 10s client cap, surfaces
as `000`, and gets retried.

**Retry arithmetic is not the sleep sum.** "5 attempts, 3s apart" reads as ~12s
and is ~12s only when the connection is *refused* (fails instantly). When the
socket hangs, the worst case is 5×timeout + 4×sleep = ~62s, and each retry
fires a fresh request the server keeps processing — 5 stacked in-flight calls.
Always state the hung-socket number, not the refused number.

**How to apply:** for every call site of a shared request helper, ask "does this
endpoint make an outbound network call?" A single shared timeout is only safe
when every call site is storage-bounded; otherwise the timeout belongs as a
per-call parameter. Related: [[offload-fix-all-callsites]],
[[datahub-unavailable-only-retryable]].
