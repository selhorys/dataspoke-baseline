---
name: rollback-noop-claim-audit
description: Auditing any added session.rollback() needs two passes — pending DML *and* Session.rollback()'s identity-map expiry, which bites async callers looping over ORM rows with MissingGreenlet
metadata:
  type: feedback
---

When a generator adds `await session.rollback()` — on a **success** path justified as "a no-op,
every step commits its own work", or on an **error** path justified as containment ("degrade the
signal, not the sweep") — verify both halves:

1. **Pending DML** — walk the callee end to end and pair every `session.add()`,
   `session.delete()`, and Core `execute()` of an INSERT/UPDATE/DELETE with the `commit()`
   that follows it, including conditionally-guarded commits (`if counter:`) and commits that
   sit *inside* an `if` block whose condition also gates the DML. A single uncommitted write
   at return means the rollback silently discards a whole run while the endpoint reports
   success.
2. **Identity-map expiry** — `Session.rollback()` expunges pending objects and **expires
   every remaining loaded object**. `expire_on_commit=False` does **not** protect it: that flag
   is read only by `_remove_snapshot` (commit); rollback goes through
   `SessionTransaction._restore_snapshot(dirty_only=False)`, which expires the whole identity
   map unconditionally. Under `AsyncSession` the next attribute access on such an object
   triggers a sync lazy refresh and raises `MissingGreenlet`. Check whether any caller (or
   test) holds an ORM object across the call.

**The error-path shape is worse than the success-path one.** A containment rollback placed
inside a helper that is called from `for row in <orm rows>:` guarantees the crash: the very next
iteration reads `row.mode` / `row.id` on a now-expired instance. So the rollback added to
*prevent* the sub-pass from killing the sweep is itself what kills it — with a different
exception type and the same health-row/DAG-500 outcome. Look for the loop, not just the helper.

**Repro that settles it in ~20 lines** (sync sqlite is enough for the expiry half):
load rows → run a statement that fails → `rollback()` → `inspect(row).expired` is `True`, and a
plain `row.attr` read emits a `SELECT`. Then `sqlalchemy.util.await_only(coro)` outside
`greenlet_spawn` shows the `MissingGreenlet` the async path would raise.

**Why:** the pending-DML half is what everyone checks; the expiry half is invisible in the
diff and only fires for a caller that happens to hold a row. Both were live questions in the
`IngestionService.sync()` / `_report_api_health` review, and the expiry half was the blocker in
the passive run-observation (`_rollback_quietly`) review.

**How to apply:** for a side-effect write, the cleaner fix is a **dedicated session**
(`SessionLocal()` / `make_db_session()`), which is what `src/shared/datahub/consumer.py`
already does for its own `peripheral_health` report. For a containment rollback inside a sweep,
the fix is to snapshot what the loops need into plain Python values *before* the sub-passes run,
so no ORM attribute is read after a rollback. Related: [[sync-sweep-no-unit-coverage]] — the
sweep has no unit coverage, so neither half is caught by a green `tests/unit/` run.

**Verifying the snapshot fix (re-review pass).** The accept criterion is not "a dataclass was
added" — it is that *every* `select()` in the rollback-capable region returns **columns, not
entities**: `select(Model.col)` leaves nothing in the identity map, `select(Model)` does. Grep the
region for `select(<Model>)` with no attribute, and for `.scalars().all()` whose rows are then
attribute-read. Expect the generator to also drop a blanket comment like "nothing below this point
touches an ORM instance" — check it literally, because prefetch dicts loaded *inside* an earlier
step (e.g. `{r.dataset_urn: r for r in result.scalars().all()}` feeding `session.delete(row)`) are
usually still ORM instances. They are harmless when no rollback intervenes, but the comment is the
guard-rail for the next edit, so an overstated one is worth a minor finding.
