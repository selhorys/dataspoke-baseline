---
name: rollback-noop-claim-audit
description: Auditing a "this rollback() is a no-op" claim needs two passes — pending DML *and* Session.rollback()'s identity-map expiry, which bites async callers with MissingGreenlet
metadata:
  type: feedback
---

When a generator adds `await session.rollback()` on a **success** path and justifies it as
"a no-op, every step commits its own work", verify both halves:

1. **Pending DML** — walk the callee end to end and pair every `session.add()`,
   `session.delete()`, and Core `execute()` of an INSERT/UPDATE/DELETE with the `commit()`
   that follows it, including conditionally-guarded commits (`if counter:`) and commits that
   sit *inside* an `if` block whose condition also gates the DML. A single uncommitted write
   at return means the rollback silently discards a whole run while the endpoint reports
   success.
2. **Identity-map expiry** — `Session.rollback()` expunges pending objects and **expires
   every remaining loaded object**, even when there was nothing to roll back. Under
   `AsyncSession` the next attribute access on such an object triggers a sync lazy refresh and
   raises `MissingGreenlet`. So a "no-op" rollback still changes the method's postcondition:
   it now returns with a fully-expired session. Check whether any caller (or test) holds an
   ORM object across the call.

**Why:** the pending-DML half is what everyone checks; the expiry half is invisible in the
diff and only fires for a caller that happens to hold a row. Both were live questions in the
`IngestionService.sync()` / `_report_api_health` review.

**How to apply:** the cleaner fix is almost always a **dedicated session** for the side-effect
write (`SessionLocal()` / `make_db_session()`), which is what `src/shared/datahub/consumer.py`
already does for its own `peripheral_health` report — then "committed independently of the
caller's transaction" is literally true instead of incidentally true, and neither half of the
audit is needed. Related: [[sync-sweep-no-unit-coverage]] — the sweep has no unit coverage, so
neither half is caught by a green `tests/unit/` run.
