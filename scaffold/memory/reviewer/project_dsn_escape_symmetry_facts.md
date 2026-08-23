---
name: dsn-escape-symmetry-facts
description: Verified escape/unescape symmetry for SQLAlchemy URL, alembic configparser, and limits/redis-py URIs — which components are covered and which are structurally uncoverable
metadata:
  type: project
---

Empirically verified (SQLAlchemy 2.x, redis-py, `limits` 5.8.0) while reviewing the
#120 credential-escaping fix. Re-deriving these costs a scratchpad script each time.

**SQLAlchemy `URL`**
- `URL.create(...)` → `create_async_engine(url)` → `dialect.create_connect_args(url)` hands
  asyncpg `user`/`password` **byte-identical**, no string round trip. Covers `@ % / ? # : space + \ ' " [ ]` and non-ASCII.
- `render_as_string(hide_password=False)` quotes username/password with `quote(safe=" +")`,
  the exact inverse of the `unquote` `make_url` applies. `quote_plus` is the **only**
  asymmetric member of the family (`pa ss` → `pa+ss` → `pa+ss`).
- `str()` / `repr()` of a `URL` mask the password as `***`. `repr(engine)` too.
- `render_as_string` does **not** quote `database` or `host`, and `make_url` does **not**
  unquote `database`. So a db name containing `?` is *structurally* uncoverable on any string
  DSN path (`.../d?b` → database `d`; `.../d%3Fb` → database `d%3Fb`). The only fix is to
  never stringify — build the engine from the `URL` object.
- Empty port raises `ValueError` on **both** the old string path and `int(port)`. Not a
  behaviour change; don't flag it.

**Alembic string path** — `render_as_string` → `.replace("%","%%")` → `set_main_option` →
`get_main_option` → `make_url` round-trips username, password and db byte-identically.
The `%%` escape is load-bearing (configparser interpolation).

**Redis / `limits`** — `limits.storage.RedisStorage.__init__` passes the URI straight to
`redis.from_url` → `ConnectionPool.from_url` → `redis.connection.parse_url`, which applies
`unquote` to username and password. So `quote(pw, safe="")` is correct and symmetric.
Pre-fix breakage confirmed: `/`, `#`, `?` in a raw password all raise
`ValueError: Port could not be cast to integer` **at import**; `@` survives (netloc splits on
the last `@`); `%` silently decodes to a different password.

**How to apply:** when reviewing anything that puts a credential in a URI, check which of the
three shapes it is (URL object / rendered string / hand-built f-string) and which parser
consumes it. See [[offload-fix-all-callsites]] for enumerating the sites.

**Redis host validation (`_format_redis_host`, `src/api/middleware/rate_limit.py`)** — added in
the same fix. Escaping the host is not an option (an IPv6 literal needs its bare `[...]`), so
the host is *rejected* instead, at module import. Verified: it also rejects a trailing-dot FQDN
(`redis.svc.cluster.local.`) even though `parse_url` handles it fine (`host='redis...local.'`) —
appending `\.?` to the pattern fixes that while still rejecting `@ / ? # : %`. The validator
lives in the middleware module, so the other two `settings.redis_host` consumers
(`src/api/main.py`, `src/workflows/_common.py` → `RedisClient` kwargs) are ungoverned.

**install.sh strand** — RESOLVED on branch `fix/119-122-131-132-helm-credential-integrity`
(2026-08-02): `_derive_airflow_metadata_secret` is now compare-and-rotate (re-derives the URI
every run, rewrites only on a diff, sets `AIRFLOW_METADATA_DSN_ROTATED` only when the prior
value was non-empty), so the `_url_encode` fix does reach an existing install. Verify before
relying on it — the branch was uncommitted when this was written.

**The `DATASPOKE_POSTGRES_USER` shape gate is still dev-only, and now structurally so**: the
value moved out of the credentials Secret into the app ConfigMap, so the prod pre-flight has
nothing to read. A sibling `DATASPOKE_POSTGRES_DB` gate was added beside it, also dev-only.
Neither is a render-time guard, so an overlay overriding `config.postgres.*` reaches prod
unshape-checked — `templates/configmap.yaml` only asserts agreement with `postgresql.auth.*`,
never shape. See [[postgres-identity-plane-facts]].
