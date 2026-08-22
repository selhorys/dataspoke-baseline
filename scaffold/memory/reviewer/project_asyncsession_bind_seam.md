---
name: asyncsession-bind-seam
description: SQLAlchemy 2.0.51 AsyncSession.bind facts — set only when a bind is passed, absent from dir(), holds the AsyncEngine (get_bind() returns the sync Engine); the helper is total, and only a non-AttributeError read logs
metadata:
  type: project
---

`independent_sessionmaker` (`src/shared/db/session.py`) derives a factory from the injected
session's `bind` + `isinstance(bind, AsyncEngine)`, falling back to the module-level
`SessionLocal`. The seam rests on four SQLAlchemy behaviours, all re-verified against 2.0.51
in this repo:

- `AsyncSession.__init__` does `if bind: self.bind = bind` — a **public instance attribute**, set
  only when a bind is passed. `AsyncSession()` with no bind has **no** `bind` attribute at all
  (`getattr(..., "bind", "MISSING")` → `'MISSING'`), so the `None` default is reachable.
- `"bind" not in dir(AsyncSession)` → `AsyncMock(spec=AsyncSession)` / `MagicMock(spec=AsyncSession)`
  do **not** expose `bind`. A bare `AsyncMock()` does, but yields an `AsyncMock`, so the
  `isinstance` guard rejects it. Both mock shapes fall through to `SessionLocal`.
- A session from `async_sessionmaker(engine, ...)` has `bind is engine` (the **AsyncEngine**).
  `AsyncSession.get_bind()` returns the *sync* `Engine` and is the wrong accessor.
- `AsyncSession(bind=<AsyncConnection>)` also lands in `self.bind`, so a connection-bound session
  (the classic rollback-per-test fixture) fails the `isinstance` check and silently reverts to the
  module-level `SessionLocal` — the exact path the binding exists to avoid. No caller does this
  today (`grep -rn "AsyncSession(bind" src/ tests/` → no match); re-check that grep if the fallback
  ever starts mattering.

Since #140 the helper is **total** — the read is `try: bind = db.bind / except AttributeError:
bind = None / except Exception: logger.warning(...); bind = None`. So four shapes fall back and
three of them do it silently (absent / `None` / not an `AsyncEngine`); only a read that raises
something *other than* `AttributeError` is logged at WARNING with `exc_info`. Consequence worth
holding onto: an `AttributeError` raised from *inside* a `bind` property is indistinguishable from
a genuinely absent attribute and takes the silent branch, which `spec/feature/BACKEND.md`'s
PostgreSQL row nominally assigns to the WARNING class. Both call sites
(`IngestionService._report_api_health`, `lookup_and_validate`'s `last_used_at` stamp) swallow and
log their own failures on top of that.

**Why:** issue #118 — a host-invoked sweep wrote its `datahub-api` health row to `localhost:5432`
because the reporter reached for the import-time `SessionLocal`. The fix keeps the *independent
session* the spec pins (`spec/feature/BACKEND.md` §Sync + mapping sweep → **Health side effect**)
while moving it onto the caller's engine. #140 then removed the read asymmetry between the two
call sites.

**How to apply:** when reviewing anything that opens a second session next to an injected one,
check which factory it reaches for, then confirm the mock/bind shapes above rather than trusting a
generator's "verified against SQLAlchemy" claim. Related: [[sync-sweep-no-unit-coverage]],
[[dsn-escape-symmetry-facts]], [[helper-made-total-vacates-rationales]].
