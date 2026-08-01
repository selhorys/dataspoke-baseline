---
name: dsn-url-fields-anchor
description: The URL-fields-not-DSN-string rule is spec/feature/BACKEND.md:131; test_session.py is the reference shape; the getsource env-key backstop is redundant and refactor-brittle
metadata:
  type: project
---

The rule "credentials are carried as `sqlalchemy.URL` fields rather than interpolated into a DSN
string … and the URL's string form masks the password" is specced at
**`spec/feature/BACKEND.md` §Shared Services, PostgreSQL row (line ~131)**. It is *not* in
`spec/TESTING.md §Integration Lifecycle & Isolation` — that section (~L333-336) only says reset
helpers read credentials from the `DATASPOKE_TEST_*` env block and hardcode none. Both citations
now appear correctly split in `tests/unit/integration_util/test_main_db_url.py`.

Reference shape: `tests/unit/shared/db/test_session.py` (~35-128) — parametrize over
`_HOSTILE_CREDENTIALS = ["p@ss", "p%2Fss", "pa ss", "p/s?s#x", "p:ss", "100%"]`, assert
`dialect.create_connect_args(url)[1]` round-trips password *and* host (the `@`-truncation pin),
plus `str`/`repr` masking. `tests/unit/migrations/test_env.py` uses the identical set.
`URL.create` exists in exactly two src/migration files: `src/shared/db/session.py`,
`migrations/env.py`; `tests/integration/util/__main__.py::_dataspoke_db_url` is the third.

**Measured, #118, and re-measured after the `getsource` test was deleted (file is now 15 tests):**
reverting `_dataspoke_db_url` to the f-string DSN kills nearly the whole file. Renaming *any one* of
the five `DATASPOKE_TEST_POSTGRES_*` keys — either to the runtime `DATASPOKE_POSTGRES_*` block or to
a typo outside both blocks — is still caught by the behavioural tests alone (1-15 failures each; the
thinnest is `PORT` → typo at 1). So an `inspect.getsource(...)`-substring "env key names are stable"
backstop adds **zero** kill power, and it is the one test that fails a behaviour-identical refactor
(keys built from an `f"DATASPOKE_TEST_POSTGRES_{field}"` prefix). Deleting it lost nothing. Flag
that shape wherever it reappears; the `populated` half of a component-wise env test already covers
it.

`_dataspoke_db_url` is annotated `-> URL` via a `TYPE_CHECKING` import (the module has
`from __future__ import annotations`); the runtime `from sqlalchemy import URL` stays function-local
so `--help` does not pay for SQLAlchemy. mypy is clean on that module.

Still raw-DSN f-strings: `tests/integration/conftest.py:118-121`,
`tests/integration/spot/test_ontogen_embedding_upserts.py`,
`tests/integration/spot/test_uc4_metagen_evidence_prompt.py`.
