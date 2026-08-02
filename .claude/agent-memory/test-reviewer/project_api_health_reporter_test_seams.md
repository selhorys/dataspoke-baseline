---
name: api-health-reporter-test-seams
description: Test seams for IngestionService._report_api_health (#118) — measured kill/tolerate table after the post-ESCALATE fix pass, and the one over-pinned assertion left
metadata:
  type: project
---

`IngestionService._report_api_health` opens its own session on `self._db.bind`
(`src/backend/ingestion/service.py`, ~1498). Measured facts for judging tests on this seam:

**Runtime facts (SQLAlchemy 2.0.51, verified):** `AsyncSession.bind` holds the **`AsyncEngine`**;
`bind` is *not* in `dir(AsyncSession)`, so `AsyncMock(spec=AsyncSession)` has no `bind` and falls
through to the guard. `AsyncSession.get_bind()` returns the *sync* `Engine` — the wrong accessor.
`getattr(obj, "bind", None)` only swallows `AttributeError`, so a `bind` **property that raises**
propagates — that is the only injected shape that makes the try-scope observable.

**Killed by `TestSyncReportsApiHealth` (re-measured after the fix pass, class only, 15 tests):**
always-`SessionLocal` (the #118 bug), reuse-the-injected-session, drop the `isinstance(AsyncEngine)`
guard, `get_bind()` instead of `.bind`, fallback-to-injected, wrong row name, `except: raise`,
**dropping `exc_info=True`** (2 fails), **deleting the swallow log** (2 fails), **hoisting the
bind/factory derivation above the `try`** (1 fail). Both cycle-2 survivors are now closed.

**The log level is now spec'd and pinned (#135).** `BACKEND.md §Health reporting` (L1561-1567)
says a reporter's own write failure is swallowed "and logged at `ERROR` with `exc_info=True`", and
§Best-Effort Operations (L1629-1632) scopes its WARNING sentence to its four table rows and
cross-references it. The rule binds **every** reporter writing the table — both
`_report_api_health` and `src/shared/datahub/consumer.py::HealthReporter`. Measured: demoting
either to `logger.warning` now fails (2 ingestion params / the consumer test). The earlier
"do not pin the level, the divergence is unresolved" note is obsolete.
**Still correctly tolerated:** `logger.error(..., exc_info=True)` → `logger.exception(...)`
(same level, same `exc_info`) — measured green on both reporters. Do not let anyone pin the
call spelling.

**Over-pinned, one assertion:** `test_reading_the_injected_sessions_bind_cannot_break_the_sweep`'s
`assert seen == []`. Measured: an impl that wraps only the `bind` read in its own
`try/except → bind = None` and still writes the row through `SessionLocal` fails there — yet it
satisfies §Health side effect *and* the sibling fallback test's own stated reading ("there is no
shape of injected session for which it stops trying to write it"). It adds no kill power: the hoist
mutation kills via the `RuntimeError` escaping `sync()` before that line runs.

**Spot tier (`tests/integration/spot/test_datahub_api_health.py`) is the real host-side regression
test.** `_session_local_on` was deleted; `async_session` is bound to `async_engine` (the forwarded
port) and `.env.dev` carries no `DATASPOKE_POSTGRES_*`, so a regressed reporter would aim at
`localhost:5432`, write nothing, and the file's `ok`/`error` backstops would fail. Do not let
anyone reinstate a `SessionLocal` patch there.

**How to apply:** re-run the kill table above when this area changes; the two log-level mutations
must stay green.
