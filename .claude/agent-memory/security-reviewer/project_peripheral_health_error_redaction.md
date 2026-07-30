---
name: peripheral-health-error-redaction
description: The three sinks for a DataHub transport error string (peripheral_health.last_error, activity 500 body, API log) and which of them src/shared/redaction.py actually covers; the 401/403 fail-fast path still bypasses the exact-value scrub
metadata:
  type: project
---

A DataHub transport exception's `str()` reaches **three sinks with three reader populations**, and
any diff touching one must be checked against all three:

1. `peripheral_health.last_error` — Admin reads it back over `GET /admin/peripherals/datahub`.
   Written only via `report_peripheral_health()` in `src/backend/admin/peripheral_health.py`, which
   is the choke point for all four row names (`datahub`, `datahub-api`, `langfuse`, `smtp`).
2. the internal activity's `500` body (`activities.py::_error_response`) — Airflow task logs.
   Only carries `str(exc)` for `DataSpokeError` subclasses; anything else escapes to Starlette's
   generic 500 (no body detail, raw traceback in the log).
3. the API's own log — `src/api/main.py::_handle_datahub` logs `str(exc)` at WARN. The 502 **body**
   is a fixed generic string, so the HTTP response was never the leak; the log is.

**Where the control lives.** `src/shared/redaction.py::sanitize_error_message(msg, *, secrets=())`
— exact-value scrub, whitespace collapse, control/Cf strip, pattern scrub. Applied at
`report_peripheral_health` (sink 1) and at both `raise DataHubUnavailableError` sites in
`src/shared/datahub/client.py` (which is the only holder of the live PAT, so it is the only caller
that can pass `secrets=`).

**Residual fail-open, unchanged.** `DataHubClient._with_retry` re-raises `401`/`403` **bare**
(`_FAIL_FAST_STATUS_CODES`), and `get_aspect` does the same. So the auth-failure exception — the one
most likely to quote the credential — never reaches the exact-value scrub. `IngestionService.sync()`
now reports `datahub-api` health on **any** exception (not just `DataHubUnavailableError`), which
fixes the "revoked PAT leaves api_health ok forever" half, but `_report_api_health` passes no
`secrets=`, so that row gets the pattern layer only.

**How to apply:** on any diff touching `last_error`, a `peripheral_health` write, or a
`DataHubUnavailableError` message, check (a) which of the three sinks it covers, (b) whether the
exact-value scrub is available at that call site and used, (c) whether the exception type that
actually fires reaches the sanitizer at all. Related: [[sanitizer-pipeline-ordering]],
[[consumer-db-plane-to-wire-boundary]], [[recipe-regex-trust-boundary]].

**Glob-list gaps** (not in the agent's sensitive-path list, but security-load-bearing):
`src/shared/redaction.py`, `src/backend/admin/peripheral_health.py`.
