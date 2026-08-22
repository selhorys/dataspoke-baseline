---
name: independent-sessionmaker-seams
description: Test seams for src/shared/db/session.py::independent_sessionmaker + the health-log-level rows (#134/#135) — cycle-2 measured mutation table; the caller's-bind rule is now specced in BACKEND.md §Shared Services
metadata:
  type: project
---

`independent_sessionmaker(db)` (`src/shared/db/session.py`) derives a factory from
`db.bind` when it is an `AsyncEngine`, else returns module-level `SessionLocal`. Callers:
`IngestionService._report_api_health` and `auth/api_tokens.py::lookup_and_validate`.

**The rule is specced now** — `BACKEND.md §Shared Services` (PostgreSQL row) carries all
three clauses verbatim: "bind of the injected session", "aimed at a different address than
every other statement in the same call", "falls back to the module-level factory". So
`stamped_on[0] is callers_engine` is a citation, not a derivation. Also specced (#135):
`§Health reporting` fixes a reporter's own write failure at `ERROR` + `exc_info=True`, and
`§Best-Effort Operations` was narrowed to "the operations listed below".

**Cycle-2 measured table** (32 mutations, unit tier, whole suite 2950 tests / ~25s):

- KILLED, all: drop `throttle_session.commit()`; stamp on the caller's session; always-
  `SessionLocal`; always-derived; `bind is not None` instead of `isinstance(AsyncEngine)`;
  bare `db.bind` (no getattr default); `db.get_bind()`; `expire_on_commit` default on the
  derived factory; remove the mirror's `except`; mirror WARNING→ERROR; mirror drop
  `exc_info`; mirror silent swallow; mirror `return 1`; `_report_api_health`
  ERROR→WARNING; drop its `exc_info`; sync reports error-on-ok / ok-on-error; consumer
  ERROR→WARNING / drop `exc_info` / silent swallow.
- TOLERATED (correct): inserting a spec-literal read-then-update SELECT on the throttle
  session — cycle 1's false kill is fixed by filtering `isinstance(stmt, Update)`.
- **FALSE KILLS (residue)** in `test_the_last_used_at_stamp_is_written_to_the_callers_database`:
  splitting the caller's join into two SELECTs (killed by `mock_db.execute.await_count == 1`,
  L599 — has real unique kill-value: a *dual* write on both sessions is caught by nothing
  else); a raw `text()` UPDATE (killed by the `Update` isinstance filter); `factory.begin()`
  with no explicit commit and a harmless double-commit (both killed by
  `committed == [stamp_session]`, inherent to `patch.object(AsyncSession, "commit")`).

**Two traps worth remembering:**

1. `test_session.py`'s `_build_url` tests `importlib.reload` the module, so a top-of-file
   `SessionLocal` import is stale by the time the `independent_sessionmaker` tests run.
   `_live_session_local()` (L242) resolves it live. Measured: swapping it back for the
   top-level name fails all 4 fallback params in-file and passes in isolation — so the
   helper is load-bearing, not decoration. `reload` reuses the module dict, so
   `patch("src.shared.db.session.SessionLocal")` still reaches the live object.
2. `TestMirrorExecutionRequestsPollIsBestEffort::test_a_failed_poll_does_not_flip_the_
   datahub_api_row_to_error` stands `_run_sweep` in. Verified honest: `_run_sweep` step 4
   (service.py:2031-2037) wraps the mirror call in **no** try/except, so the mirror's own
   `except` really is the sole containment. But removing it makes the test fail by the
   exception *escaping*, not by the row reading `error` — the docstring's claim is wrong,
   and the test kills nothing that its sibling or `TestSyncReportsApiHealth` does not.

Related: [[api-health-reporter-test-seams]], [[dsn-url-fields-anchor]].
