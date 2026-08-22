---
name: independent-sessionmaker-silent-fallback
description: independent_sessionmaker's SessionLocal fallback is silent for every shape except a non-AttributeError bind read; plus the WARNING-vs-ERROR criterion BACKEND.md codifies for best-effort writes, and the measured reason neither level nor its extra={} fields survive to the deployed stream
metadata:
  type: project
---

`src/shared/db/session.py::independent_sessionmaker(db)` decides which **database** an
"independent" write lands on. Facts that keep getting missed on diffs that touch it
(measured 2026-08-03, re-measured against #140's third fix pass 2026-08-04):

- **Silent fallback is the default; one shape logs.** `isinstance(bind, AsyncEngine)` fails →
  returns the module-level `SessionLocal`, bound at import time to the app-runtime
  `DATASPOKE_POSTGRES_*` values. Absent / `None` / non-async-engine binds emit **0 log records**,
  so a write the docstring says must "reach the database the caller is actually using" can land
  on a *different* database with no diagnostic. `AsyncConnection`-bound sessions (the common
  transactional-test fixture shape) take that silent path. Only a `bind` read raising something
  other than `AttributeError` logs `logger.warning("independent_sessionmaker_bind_unreadable",
  exc_info=True)`.
- **The `except AttributeError` branch is a conflation, and it is measurable.** It is meant to
  mean "no bind was passed", but an `AttributeError` raised *from inside* a `bind` property or a
  `__getattr__` chain lands there too and is silenced. `AttributeError.name` discriminates
  (measured on 3.13: `'bind'` for the genuinely-missing attribute vs the inner attribute name for
  a property that raises); `.obj` does **not** — it is the target object in both. Precise guard:
  `except AttributeError as exc: if exc.name != "bind" or exc.obj is not db: logger.warning(...)`.
  As of #140 the code docstring documents the exception but **BACKEND.md's PostgreSQL row still
  states the unqualified rule** ("a bind whose read fails for any other reason is logged at
  WARNING"), so spec and code disagree until someone signs off on one or the other.
- `AsyncSession.bind` is a plain instance attribute (`if bind: self.bind = bind` in `__init__`,
  SQLAlchemy 2.0.51), never a property — so the conflation has no live trigger in `src/` and is a
  contract/observability defect rather than an exploitable one. Re-check that if the helper ever
  starts taking a proxy or subclass.
- **Ask on any diff here: does the helper swallow something the caller claims to log?** The
  helper is total — it never propagates — so both call sites'
  `except Exception: logger.{warning,error}(..., exc_info=True)` guards sit *above* a swallow they
  cannot see.

**Two pooled connections per PAT request, and no `pool_timeout`.** `rg pool_timeout src/` → no
hits, so SQLAlchemy's 30s default governs. Every `dsk_`-authenticated request
(`privilege.py` → `lookup_and_validate`) holds its request-scoped connection through the token
SELECT and then asks the same `pool_size=10, max_overflow=5` pool for a second one for the
`last_used_at` stamp. At ~15 concurrent PAT requests the pool is fully held by caller sessions and
every request blocks 30s on its second checkout. Since #140 that ends in a **200**, not a 500 — the
status-code signal for pool exhaustion on the auth hot path is gone, and `last_used_at` quietly
stops advancing under exactly the load where usage evidence matters most.

**WARNING-vs-ERROR criterion** (`spec/feature/BACKEND.md` §Best-Effort Operations lead-in +
§Health reporting): WARNING is for a failure that "degrades a single operation" and sits outside
the operator's fault surface; ERROR is for a lost write to a row that is *itself* that surface,
where a stale value is indistinguishable from "nothing happened". `api_tokens.last_used_at` is
the one row in the best-effort table that logs at ERROR: nothing in `src/` reads it in band (only
`GET /auth/api-tokens`, `GET /admin/users/{id}/api-tokens`, and the profile/tokens UI render it),
so it is credential-usage *evidence*, and #140 made a stale value stop being proof of non-use.
AUTH.md §Audit now states that consequence explicitly.

**Neither the level nor `extra={}` survives to the deployed stream.**
`docker-images/api/Dockerfile:32` runs `uvicorn src.api.main:app --no-access-log` with no
`--log-config`, and nothing in `src/` calls `basicConfig` / `dictConfig`. Measured under uvicorn's
own `LOGGING_CONFIG`: root has **no handlers** at level WARNING, so app-module records fall to
`logging.lastResort` (`_StderrHandler`, WARNING, bare `%(message)s`). WARNING/ERROR do reach
stderr with the traceback, but with **no level, no logger name, no timestamp — and every
`extra={...}` key silently dropped**, because `%(message)s` never references them. That last part
bites ~20 call sites across `main.py`, `admin.py`, `rate_limit.py`, `reset.py`, `activities.py`
and `api_tokens.py`: the `user_id` / `token_id` / `endpoint` an operator would triage by is not in
the output at all. An ERROR audit-loss record and a WARNING bind record render identically.
Weigh any spec sentence of the form "the ERROR log record is the only trace" against that; the
in-scope workaround is to interpolate the id into the message (`"...token_id=%s", token_id`)
rather than pass it via `extra`.

**`text(f"...")` is extinct in `src/`** — and never shipped: the committed baseline was a *static*
literal `text("INTERVAL '60 seconds'")` (no interpolation, but it duplicated
`_LAST_USED_THROTTLE_SECONDS`). #140 replaced it with
`func.make_interval(0, 0, 0, 0, 0, 0, _LAST_USED_THROTTLE_SECONDS)`. Compiled locally it renders
`make_interval(%(make_interval_1)s … %(make_interval_7)s)` with `60` in the `secs` slot and the
row id bound (int4 → float8 is an implicit Postgres cast, so the single overload resolves). It has
**never executed against real Postgres**, and the new swallow makes a resolution failure return
200 forever — the spot test is the only control. Re-grep `rg -n 'text\(f"' src/` on any diff here:
zero hits is the bright line that makes this class greppable, and every remaining `text()` in the
tree is a static string.

Related: [[auth-credential-carrier-inventory]], [[credential-uri-escaping-boundary]],
[[peripheral-health-error-redaction]].
