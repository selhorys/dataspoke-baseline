---
name: dsn-url-fields-anchor
description: The URL-fields-not-DSN-string rule is spec/feature/BACKEND.md:131; test_session.py is the reference shape; the integration layer now has one shared builder plus the conftest-fixture seam
metadata:
  type: project
---

The rule "credentials are carried as `sqlalchemy.URL` fields rather than interpolated into a DSN
string … and the URL's string form masks the password" is specced at
**`spec/feature/BACKEND.md` §Shared Services, PostgreSQL row (line ~131)** — verbatim, verified
twice. It is *not* in `spec/TESTING.md §Integration Lifecycle & Isolation` — that bullet (~L333-336)
is scoped to **reset helpers** ("Reset helpers fail loud and carry no baked-in credentials … read
all credentials from the environment (the `DATASPOKE_DEV_*` block)"). For a *conftest fixture*
reading that block, the on-point anchor is **`spec/TESTING.md` §Running (~L397)** — "`conftest.py`
and `util/*.py` consume the `DATASPOKE_DEV_*` block it contains". Citing L335 for a fixture is a
scope stretch; §Running is the precise one. Same for `util/db_url.py::dataspoke_db_url`, whose
second caller (`datahub.py::sync_dataset_registry`) is a provisioning reconcile, not a reset.

Reference shape: `tests/unit/shared/db/test_session.py` (~35-128) — parametrize over
`_HOSTILE_CREDENTIALS = ["p@ss", "p%2Fss", "pa ss", "p/s?s#x", "p:ss", "100%"]`, assert
`dialect.create_connect_args(url)[1]` round-trips password *and* host (the `@`-truncation pin),
plus `str`/`repr` masking. `tests/unit/migrations/test_env.py` and
`tests/unit/integration_util/test_dataspoke_db_url.py` use the identical 6-member set.

**The 6-member set is not padding — measured.** With the set thinned to `["p@ss/word", "100%"]`,
`password=unquote(password)` and `username=unquote(user)` (the read-side half of a DSN escape
asymmetry) both SURVIVE: `unquote("100%") == "100%"` and `unquote("p@ss/word")` is unchanged, so
only `p%2Fss` exercises the percent-*decode* direction. `100%` covers the *encode* direction
(`quote`) only. Keep `p%2Fss` in any set that claims to pin "a `%` decodes into a different
password entirely".

**Integration-layer state after #133:** no raw-DSN f-string remains anywhere in `tests/`, `src/`,
`migrations/`. The two spot `_dsn()` helpers are gone; `tests/integration/util/db_url.py::
build_postgres_url` is the shared builder for both `tests/integration/conftest.py::
integration_db_url` (required host/port/user/password, `DB` defaults to `dataspoke`) and
`tests/integration/util/db_url.py::dataspoke_db_url` (all five defaulted; used by the reset CLI **and** `datahub.py::sync_dataset_registry`). The other
`util/*.py` reset helpers pass keyword args straight to `asyncpg.connect`, so they never had a
DSN to escape.

**The fixture's only gate is a unit test.** Measured: reverting `integration_db_url` to the
f-string form leaves the *whole* unit suite green (2933 passed) except
`tests/unit/integration_conftest/test_integration_db_url.py`, which fails 3. Nothing else observes
the fixture's return value and `tests/` is not type-checked, so the `-> URL` annotation is no
backstop. The seam: `importlib.util.spec_from_file_location` + `exec_module` on
`tests/integration/conftest.py` under a throwaway module name, inside
`patch.dict(os.environ, _IMPORT_ENV, clear=True)`, then call
`module.integration_db_url.__wrapped__()`. Verified safe — `os.environ` is restored *exactly*
(patch.dict contains `_load_dotenv()`), hostile ambient `DATASPOKE_DEV_POSTGRES_*` cannot leak,
and the module's five required import-time env reads (conftest L94/97/98/101/102) are exactly the
five keys `_IMPORT_ENV` supplies. Residue: the exec freezes `tests.integration.util.{datahub,
postgres,kafka}` module-level constants from `.env.dev`-on-disk; harmless unless unit and
integration run in one process with unit first (a bare `pytest` is safe — `integration` sorts
before `unit`).

An `inspect.getsource(...)`-substring "env key names are stable" backstop adds **zero** kill power
over the behavioural tests and is the one shape that fails a behaviour-identical refactor. Flag it
wherever it reappears.
