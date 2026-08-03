---
name: last-used-at-stamp-seams
description: Measured mutation table for the best-effort api_tokens.last_used_at stamp (#140) — the WHERE row filter and predicate legs are now killed at the unit tier by a clause-tree evaluator; what is still deliberately free
metadata:
  type: project
---

`lookup_and_validate` (`src/backend/auth/api_tokens.py`) stamps `last_used_at` on a session
from `independent_sessionmaker`, best-effort: any failure is swallowed and logged at **ERROR**
with `exc_info` (the one carve-out in `BACKEND.md §Best-Effort Operations`, whose WARNING
default `§Health reporting` re-argues). Predicate is
`func.make_interval(0,0,0,0,0,0,_LAST_USED_THROTTLE_SECONDS)` — bound params, no `text()`.

**The seam** (`tests/unit/api/auth/test_api_tokens.py`): a test-local SQL evaluator
(`_sql_eval` / `_sql_function_value` / `_stamp_matches` / `_stamp_set_clause`) walks the
`Update`'s clause tree in SQL three-valued logic against seeded rows, with `_STAMP_NOW` standing
in for `now()`. It **raises on any node/function it does not recognise**, so a respelled
predicate fails loud instead of being silently skipped. Read the WHERE by *meaning*: operand
order and conjunction structure are free.

**Cycle-2 measured (reviewer, `tests/unit/api/auth/test_api_tokens.py`, 23 tests / 0.08s).
KILLED — the whole former survivor list is now closed at the unit tier:** drop
`ApiToken.id == token_id`; `ApiToken.id` → `ApiToken.user_id`; `id == id` (constant-true);
drop the WHERE; `<`→`>`; `<`→`!=`; `is_(None)`→`is_not(None)`; drop the stale OR-leg;
`.values(last_used_at=None)`; `.values(created_at=...)`; `.values(last_used_at=..., created_at=...)`;
a stale SET value (`now() - 1 hour`); window 60→30 and 60→3600; the seconds arg moved to the
`mins` slot. Still killed from cycle 1: drop `commit()`; ERROR→WARNING; drop `exc_info`;
`extra=` instead of the `%s` message; silent swallow; `return token.id` (re-read); stamp on the
caller's session.

**Deliberately free (do not "fix"):**

1. `<` → `<=` — the boundary at exactly 60s is undetermined by `AUTH.md` ("no-op **below** 60s");
   rows are seeded at 59s/61s, not at 60s.
2. Hoisting `token_id = token.id` into the `try` — documented unkillable in
   `test_the_stamp_handler_cannot_raise_from_reading_the_token_id`; both shapes are the same 500.
3. `func.now()` → `func.current_timestamp()` — the evaluator treats the now-family as one value.

**Residue:** `.values(last_used_at=datetime.now(UTC))` (a Python-side clock) **fails** —
defensible (the WHERE compares against DB `now()`, so mixing clocks skews the window) but the
test states no reason, so it currently reads as a spelling pin. And nothing in *any* executed
tier has yet run this predicate against real Postgres: `make_interval`'s 7 untyped bind params in
`timestamptz < now() - make_interval(...)` are proven only by
`tests/integration/spot/test_auth_api_tokens.py::test_pat_authentication_stamps_last_used_at_and_then_throttles`,
which still needs a green cluster run.

`test_reading_the_injected_sessions_bind_cannot_break_the_sweep`'s `seen ==
[("datahub-api","ok")]` is the **sole** killer of a propagating `independent_sessionmaker`;
`summary == {...}` alone survives it (the reporter's own `except` absorbs the raise).

Related: [[independent-sessionmaker-seams]], [[auth-serialization-untested-rows]].
