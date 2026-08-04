---
name: auth-prelock-postlock-401-indistinguishable
description: A post-lock credential re-check refusal is byte-identical to the ordinary pre-lock auth-gate refusal (401/UNAUTHORIZED/"Session is no longer valid."), so no response field can prove which fired — only an observed lock wait can
metadata:
  type: project
---

`src/api/auth` raises the **same** failure from two different points in a request:

- pre-lock gate — `privilege.require_authenticated` (`src/backend/auth/privilege.py`, the
  `session_epoch_matches(payload["ses"], user.session_epoch)` branch)
- post-lock re-check — `privilege.revalidate_under_user_lock` (the
  `session_epoch_matches(ctx.session_epoch, user.session_epoch)` branch)

Both are `AuthenticationError(error_code="UNAUTHORIZED", message="Session is no longer valid.")`
→ `401` with an identical `{error_code, message, trace_id, resp_time}` envelope. The PAT variants
collide too: `api_tokens.lookup_and_validate` and `revalidate_under_user_lock` both raise
`TOKEN_REVOKED`.

**Why:** any test claiming to prove AUTH.md §Serialization of credential-creating writes
("re-checks, **under** the lock") must show the request's authorising read happened *before* the
competing bind committed. The status code, error code, and message cannot show that. The only
in-band evidence is an **observed lock wait** while the blocker is still uncommitted.

**How to apply:** when reviewing a serialization/race test over HTTP, the load-bearing assertion is
the blocked-ness observation, not the 401. Check it is *pinned*: an unpinned "some backend is
blocked by pid N" lets a request that authenticated *after* the commit satisfy every remaining
assertion. Pin it by requiring exactly one waiter plus a pre-dispatch check that the holder blocks
nobody, and/or by narrowing on `pg_stat_activity.query`.

Technique notes that hold up:
- `pg_blocking_pids(pid)` is EXECUTE-to-PUBLIC and `pid` is never masked, so it works across roles;
  `query`/`state`/`wait_event*` are masked for other roles. In this dev env
  `DATASPOKE_DEV_POSTGRES_USER` is populated from the same `dataspoke-secrets` role the API pod
  uses, so `query` *is* readable here — usable as an additive narrowing, not as the primary signal.
- `pg_stat_activity` is snapshotted per transaction (`pgstat_clear_snapshot` at txn end), so a
  polling loop must roll back every iteration or it re-reads the first observation forever.
- The dev cluster sets no `statement_timeout`, `lock_timeout`, or
  `idle_in_transaction_session_timeout`, and no custom ingress proxy timeout — a holder may sit
  idle-in-transaction for tens of seconds without being reaped.

Related: [[auth-serialization-untested-rows]], [[populate-existing-alias-fake-trap]].
