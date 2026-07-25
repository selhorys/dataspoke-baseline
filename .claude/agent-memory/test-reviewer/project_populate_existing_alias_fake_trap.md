---
name: populate-existing-alias-fake-trap
description: Query-routing fakes that hand back a *different* object for the FOR UPDATE read cannot catch a dropped epoch/state snapshot — lock_user uses populate_existing and returns the same instance, so the fake must mutate in place
metadata:
  type: project
---

`src/backend/auth/users.lock_user` (and any `select(...).execution_options(populate_existing=True)`
read) returns **the same ORM instance** the earlier plain read produced, with its attributes
refreshed in place. Impl code therefore snapshots the pre-lock value into a local
(`epoch_at_read = user.session_epoch` in `reset.py::issue_reset_token`) — comparing
`locked.session_epoch != user.session_epoch` would compare an attribute against itself and
never fire.

A query-routing fake that models the lock read as a **separate object** with a different epoch
inverts this: the buggy no-snapshot comparison also declines, so the test passes against it.
Verified by probe — the fake must instead mutate the *same* instance when it sees the
`FOR UPDATE` statement.

**Why:** issue #75 cycle-2 review; `tests/unit/api/auth/test_reset.py::_ResetRoutingSession`
took a `locked_user=<distinct object>` parameter and its decline test silently passed against a
snapshot-less clone of `issue_reset_token`.

**How to apply:** on any test whose fake session serves a `FOR UPDATE` / `populate_existing`
re-read, check whether it returns a new object or mutates the original. New object ⇒ ask which
impl bug the test claims to catch, and whether aliasing would have hidden it.
Related: [[auth-serialization-untested-rows]].
