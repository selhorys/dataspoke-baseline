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

**Two controls that do NOT reach an existing install** (both still open as of 2026-08-01):
- `_derive_airflow_metadata_secret` returns early when `dataspoke-airflow-metadata-db` already
  exists — dev (`install.sh:1295`, `:1560`) and prod (`:2166`). So the `_url_encode` fix is
  inert on every upgrade, and a Postgres password rotation never propagates to Airflow.
  `_ensure_airflow_key_secrets` has the correct derive/compare/re-apply/roll shape to copy.
- `sslmode=disable` and the host `dataspoke-postgresql:5432` are hardcoded in that same URI on
  the prod path too.

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
