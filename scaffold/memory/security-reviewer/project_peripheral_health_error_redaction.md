---
name: peripheral-health-error-redaction
description: The three sinks for a DataHub transport error string (peripheral_health.last_error, activity 500 body, API log), which of them src/shared/redaction.py covers, and why binding the datahub-api reporter's session to the caller's engine is safe (no user-supplied datasource ever becomes an AsyncSession)
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
`src/shared/datahub/client.py`.

**The 401/403 gap is closed for the `datahub-api` row.** `DataHubClient._with_retry` still re-raises
`401`/`403` bare (`_FAIL_FAST_STATUS_CODES`), so that exception never crosses the client's own
boundary scrub — but `IngestionService._describe_failure` re-routes it through
`DataHubClient.sanitize()` explicitly, and `sanitize` passes `secrets=self._redact_values`
(= the PAT **and** the gms_url userinfo password). So `last_error` for `datahub-api` gets the
exact-value layer plus `report_peripheral_health`'s pattern layer plus a 1024-char cap.
Sinks 2 and 3 do **not** get that re-route.

**Reporter session binding (#118, 2026-08-01).** `_report_api_health` opens a session of its own —
so the `error` report outlives the sweep's re-raise — but now builds it on
`getattr(self._db, "bind", None)` when that is an `AsyncEngine`, falling back to the module-level
`SessionLocal`. Why this is not a "write to an attacker-chosen database" hole: `src/` contains
exactly **one** `create_async_engine` (`src/shared/db/session.py:43`), and both injection points
(`src/api/dependencies.py::get_db`, `src/workflows/_common.py::make_db_session`) use its
`SessionLocal` — so in every production path `db.bind is engine` and the branch is a no-op. The one
*user-supplied* datasource credential path, `src/backend/ingestion/extractors.py::asyncpg.connect`,
is a raw asyncpg connection and never becomes an `AsyncSession`. Measured on SQLAlchemy 2.0.51:
`AsyncMock(spec=AsyncSession)` has **no** `bind`, a bare `AsyncSession()` binds to `None`, a plain
`MagicMock().bind` fails `isinstance(..., AsyncEngine)` — the guard cannot be tricked into a
nonsense factory. `str(AsyncEngine.url)` masks the password as `***`, so the reporter's
`exc_info=True` log cannot carry a DSN credential. Note the fallback is **silent**: nothing
distinguishes "reported on the caller's engine" from "reported on the import-time engine".

**How to apply:** on any diff touching `last_error`, a `peripheral_health` write, or a
`DataHubUnavailableError` message, check (a) which of the three sinks it covers, (b) whether the
exact-value scrub is available at that call site and used, (c) whether the exception type that
actually fires reaches the sanitizer at all. A diff that merely *makes a previously-failing write
succeed* promotes the redaction chain from dead code to load-bearing — re-verify it then too.
Related: [[sanitizer-pipeline-ordering]], [[consumer-db-plane-to-wire-boundary]],
[[recipe-regex-trust-boundary]], [[credential-uri-escaping-boundary]].

**Glob-list gaps** (not in the agent's sensitive-path list, but security-load-bearing):
`src/shared/redaction.py`, `src/backend/admin/peripheral_health.py`, `src/shared/db/session.py`.
