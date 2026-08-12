---
name: alembic-advisory-lock-ordering
description: Serializing concurrent alembic runs only works from env.py — inside upgrade() the lock is after the version read; the env.py placement is verified correct in this repo
metadata:
  type: project
---

`pg_advisory_xact_lock(...)` as the first statement of a migration's `upgrade()` does **not** make
concurrent init containers safe. Verified against the installed alembic
(`MigrationContext.run_migrations`): the order is `get_current_heads()` → `_ensure_version_table()`
→ *then* `step.migration_fn()` (i.e. `upgrade()`). Two concurrent runs both read an empty
`alembic_version` and both race on **creating** it before either reaches a lock in the body; the
loser then executes the full migration against a populated schema and dies on the first plain
`CREATE`.

**The working placement, now in the tree:** `migrations/env.py` `do_run_migrations` takes the lock
inside `context.begin_transaction()` and *before* `context.run_migrations()`. Three facts make that
correct, each checked rather than assumed:

- On PostgreSQL `impl.transactional_ddl` is True and `transaction_per_migration` defaults to False,
  so `begin_transaction()` opens a **real** connection transaction (`_ProxyTransaction`), not a
  `nullcontext`. The per-migration `begin_transaction(_per_migration=True)` inside `run_migrations`
  is then the no-op one.
- `_in_external_transaction` is False on this path — `env.py` hands `run_sync` a freshly-connected
  connection with no statement executed yet, so nothing has autobegun.
- Default isolation is READ COMMITTED, so the loser's post-lock `get_current_heads()` takes a fresh
  snapshot and sees the winner's committed head row. Under REPEATABLE READ it would not.

**Why:** the req3 run needed serialization because `CREATE INDEX IF NOT EXISTS` is not race-safe
(existence check precedes name reservation → `pg_class_relname_nsp_index` collision), and migrations
run as an init container on **every** API replica. The first attempt put the lock in
`001_initial_schema.py` with a comment claiming "the loser then finds the version row already set",
which the call order contradicts; the second moved it to `env.py`.

**How to apply:** when a migration diff adds concurrency protection, check *where* in alembic's call
order the guard sits, then check the three facts above — a lock in a `begin_transaction()` that
degrades to `nullcontext` (non-transactional-DDL backend, `transaction_per_migration=True`, or an
externally-supplied in-transaction connection) is released immediately and protects nothing.
