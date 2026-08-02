---
name: credential-uri-test-seams
description: Two-cycle mutation-survival table for the #120/#117 credential-escaping tests, plus the fresh-module import seam and the hanging-guard-test trap
metadata:
  type: project
---

Measured on the `fix/credential-uri-escaping-consumer-retry` change set (session.py `URL.create`,
rate_limit.py `quote(safe="")`, consumer.py bootstrap-read `try/except`, migrations/env.py `URL`).

**Cycle 2 result: 26 mutants applied, 23 KILLED.** All four cycle-1 survivors are now dead
(module-level `storage_uri` raw f-string; `RATE_LIMIT_REDIS_DB = 0`; deleting the fault
`asyncio.sleep`; `health.report` added to the `conn is None` branch), plus `quote_plus` in
migrations/env.py, both limiters given a raw URI, driver-name swaps, credential swap, every
fallback default, and `_FAULT_RETRY_SLEEP_S != _UNCONFIGURED_SLEEP_S`.

**Non-kills that remain:**
1. `except Exception` -> `except BaseException` on the bootstrap read **HANGS** the suite
   instead of failing. `test_a_base_exception_from_the_config_read_still_escapes` shortens
   `_FAULT_RETRY_SLEEP_S` to 0.001 but does not mock `asyncio.sleep`, so a swallowed sentinel
   is an infinite 1ms retry loop. No `pytest-timeout` in this repo, so `uv run pytest
   tests/unit/` never returns. Fix = mock the sleep with an `AsyncMock(side_effect=AssertionError(...))`,
   like the two sibling tests in that file already do.
2. Dropping the `if password else ""` guard in `_build_storage_uri` SURVIVES.
   Measured: `parse_url("redis://:@h:6380/1")` == `parse_url("redis://h:6380/1")` ==
   `{'host','port','db'}` — redis-py drops an empty password before AUTH, so
   `test_storage_uri_without_a_password_carries_no_credential`'s stated failure mode does not
   exist and its assertion cannot fail. Assert on the URI **text**, not on `parse_url`.
3. `port=int(port)` -> `port=port` is equivalent — SQLAlchemy's `URL._assert_port` coerces a
   `str` port to `int` before it reaches the driver. **Re-confirmed at a second site (#133,
   `tests/integration/util/db_url.py::build_postgres_url`): the mutation survives all 24 tests
   across the three builder-test files.** Not a test gap. Both `src/shared/db/session.py::
   _build_url` and the integration builder keep the `int()` as documented explicitness/forward
   guard; do not ask for a test that pins it, and do not treat its survival as a finding —
   just check the code says so.

**Seam that works** for testing the public `storage_uri` without `importlib.reload` (reload resets
`_AUTH_LIMITED_ENDPOINTS`, which the auth-router decorators populate once at import): load a second
copy under a throwaway name via `spec_from_file_location` + `exec_module`, never inserted into
`sys.modules`, with `patch.object(settings, ...)` around the exec. Adopted and verified.

**Randomised sweep** (3000 random 1-12 char passwords over `string.printable` + non-ASCII):
zero mismatches on both `parse_url(_build_storage_uri(...))["password"]` and
`create_connect_args(_build_url(...))["password"]` — the impl is genuinely correct across the
character space, so a Hypothesis round-trip property would be cheap and would hold.

**Untested by design**: `migrations/env.py`'s three module-level branches (configparser write,
offline render, `isinstance(_url, URL)` engine build) need a real alembic `EnvironmentContext`.
The `%`->`%%` doubling in them round-trips correctly today (verified through configparser +
`make_url` for `100%`, `p%ss`, `%(here)s`), so that is regression risk, not a live bug.

**Why:** these fixes have no HTTP-observable surface, so mutation probing is the only way to tell
a real regression test from a decorative one.
**How to apply:** on any re-review, re-run the three non-kills above rather than re-reading
docstrings. Restore impl files from a `cp` backup, never `git checkout`. See also [[review-method]]
and [[no-destructive-git-during-review]].
