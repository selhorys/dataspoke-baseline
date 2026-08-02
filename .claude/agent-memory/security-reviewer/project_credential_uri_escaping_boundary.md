---
name: credential-uri-escaping-boundary
description: The four DSN/URI construction sites that carry DataSpoke credentials, which of them hold a URL object vs a rendered string after the #120 fix, the verified escape/unescape symmetry facts, and the two controls that still do not reach an existing install
metadata:
  type: project
---

Four places assemble a credential into a connection URI. After the #120 fix (2026-08-01,
branch `fix/credential-uri-escaping-consumer-retry`) the split is:

| Site | Form | Escaping |
|---|---|---|
| `src/shared/db/session.py::_build_url` | `sqlalchemy.URL` object, never stringified | none needed — dialect hands asyncpg the fields verbatim |
| `migrations/env.py::_resolve_url` | `URL` object on the **online** path (`create_async_engine(_url)`); rendered string only for `run_migrations_offline` | online: verbatim. offline: user+password quoted, **host and database raw** |
| `src/api/middleware/rate_limit.py::_build_storage_uri` | f-string (`limits` demands a str) | `quote(password, safe="")` + `_format_redis_host` **rejects** any host that is not a bare hostname / IPv4 / IPv6 literal |
| `helm-charts/bin/install.sh::_url_encode` | shell, Airflow metadata URI | `python3 urllib.parse.quote(safe="")` on user **and** password |

No raw `postgresql://` / `postgresql+asyncpg://` f-string and no `quote_plus` remains anywhere
in `src/` or `migrations/` — re-verify with that grep pair on any future DSN diff.

**`tests/` still has six of them** (verified 2026-08-01): `tests/integration/conftest.py:123`,
`tests/integration/util/__main__.py:251,289,332`, `tests/integration/spot/
test_ontogen_embedding_upserts.py:47`, `tests/integration/spot/test_uc4_metagen_evidence_prompt.py:84`
— all interpolating `DATASPOKE_TEST_POSTGRES_PASSWORD` raw, the exact bug `5d339fa` fixed in
`src/shared/db/session.py`. They carry a live dev-cluster credential, so an `@`/`/`/`%` in it
silently retargets the DSN. Any plan that fixes "the" test DSN should be checked for covering all
six, not one.

**Verified library facts** (measured against the repo's pinned SQLAlchemy / redis-py):
- `URL.create` -> `create_async_engine` -> `dialect.create_connect_args` delivers `user` and
  `password` byte-identical for `@ % / ? # : space + newline` and non-ASCII.
- `render_as_string` escapes username/password with `quote(x, safe=" +")`; `make_url` unescapes
  with `unquote` — symmetric. `quote_plus` is the one asymmetric member of the family.
- `render_as_string` does **not** quote `host` or `database`. `database="d?ssl=disable"`
  round-trips as `database='d'` + `query={'ssl':'disable'}`, which the asyncpg dialect forwards
  as **driver connect kwargs** — connect-arg injection (TLS downgrade) from an env var. Now
  reachable only via `alembic upgrade --sql` (offline), which no deployment runs.
- redis-py `parse_url` unquotes the password with `unquote`, so `quote(safe="")` is symmetric;
  measured verbatim for `p@ss p%2Fss "pa ss" p/s?s#x p:ss 100% \n π`.
- `_format_redis_host` measured: rejects `r@evil h/x h?x h#x h:6380 h%40x "" [] "a\nb"` and
  **also `redis.example.com.`** (a legal absolute FQDN — availability nit). Brackets a bare
  IPv6 literal, including a `%zone` id, which redis-py parses back correctly.
- `str()`/`repr()`/f-string of a `URL` mask the password as `***`.
- Kwargs-based (no URI, immune by construction): `src/shared/cache/client.py`,
  `src/backend/ingestion/extractors.py::asyncpg.connect` — the latter is the only
  *user-supplied* datasource credential path.

**Control status on an existing install** (re-checked 2026-08-02, branch
`fix/119-122-131-132-helm-credential-integrity`):
- **CLOSED** — `_derive_airflow_metadata_secret` is now compare-and-rotate: it re-derives the
  URI every run and rewrites `dataspoke-airflow-metadata-db` only on a difference, setting
  `AIRFLOW_METADATA_DSN_ROTATED` so `_restart_airflow_key_consumers` rolls the four Airflow
  components. So `_url_encode` finally reaches upgrades. Side effect to remember: a credentials
  Secret whose `DATASPOKE_POSTGRES_PASSWORD` has drifted from the live role's actual password
  (bitnami sets it only at initdb) now **overwrites a working DSN and restarts Airflow**.
  `helm-charts/README.md` documents the required manual `ALTER ROLE`.
- **STILL OPEN** — `sslmode=disable` and the host `dataspoke-postgresql:5432` are hardcoded in
  that same URI on the prod path too, with no override.
- The DSN's **role** is now the hardcoded literal `dataspoke` (`_url_encode "dataspoke"`), not
  read from the Secret or the ConfigMap — despite what `spec/feature/HELM_CHART.md` claims.
  The password is the only varying component. `_url_encode` feeds python via **stdin**
  (`printf '%s' | python3 -c`), so no credential reaches argv.

**Claimed mitigations that do not exist:** "dev pre-flight rejects odd db names" is false.
`install.sh:1807` shape-gates `DATASPOKE_POSTGRES_USER` (`^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`) only
inside the **dev** branch, immediately before a `GRANT ... TO ${user}`; prod never runs it.
There is **no shape check on `DATASPOKE_POSTGRES_DB`, `_HOST`, or `DATASPOKE_REDIS_HOST`** in
either profile — `_format_redis_host` is now the only one, and it lives in the API, not the
installer. `alembic.ini` still ships `postgresql+asyncpg://dataspoke:dataspoke@localhost:5432/`
as the fallback when `DATASPOKE_POSTGRES_HOST` is unset.

**How to apply:** on any diff that builds a connection URI, ask which component is escaped and
which is merely *assumed benign*. "The password is now quoted" is half a fix if host/database
sit in the same f-string. Then ask whether the fix reaches an install that already exists.
Related: [[operator-runbook-is-credential-surface]], [[install-sh-preflight-gate-mechanics]],
[[peripheral-health-error-redaction]].
