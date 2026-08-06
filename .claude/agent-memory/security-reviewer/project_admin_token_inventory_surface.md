---
name: admin-token-inventory-surface
description: The deployment-wide admin API-token inventory read — why field-by-field item construction is the only thing keeping token_hash off the wire, and the two echo paths that make it load-bearing
metadata:
  type: project
---

`GET /admin/api-tokens` (and the per-user `GET /admin/users/{id}/api-tokens`) are the
cross-user credential-inventory reads. Both are backed by
`api_tokens.list_all(...)`, whose statement is `select(ApiToken, User.email)`.

**The hash is fetched.** Compiling that statement shows
`dataspoke.api_tokens.token_hash` in the SELECT list — up to `limit` (max 1000)
hashes materialised into the request session per page. Nothing serialises it,
and the load-bearing reason is narrow: `_admin_token_to_item` in
`src/api/routers/admin.py` builds `AdminApiTokenItem` **field by field**, and the
model declares no hash field. A refactor to `model_validate(row, from_attributes=True)`
plus a hash-named field spills the whole page. Treat any edit to that helper or to
`AdminApiTokenItem`'s field list as a credential-exposure change.

**Two echo paths that make construction-site field selection the real control:**
- `src/api/main.py:_handle_validation` returns `str(PydanticValidationError)` at 422,
  and pydantic v2's `__str__` embeds `input_value=`. Any model built in a handler
  body from DB values is therefore an echo path for whatever was passed in — same
  class as [[api-422-echoes-rejected-input]], but for handler-side construction
  rather than request parsing.
- ORM instances reaching a log/exception do **not** spill: `Base` is a bare
  `DeclarativeBase` with no `__repr__`, so the default `<... object at 0x...>` is
  all a repr yields. Do not rely on this if anyone adds a `__repr__`.

**Authorization shape.** `src/api/routers/admin.py` attaches `require_admin` at the
`APIRouter(prefix="/admin", dependencies=[...])` level, so a new `@router.get` inherits
it with no per-route guard. The sibling `internal_router` (`/internal/admin`, X-Internal-Token,
`include_in_schema=False`) carries **no** token route — mirroring a credential
inventory there would widen exposure per [[internal-surface-exposure-model]].
`require_admin` reads `effective_role`, so an Admin-snapshot `dsk_` PAT reaches this
route: a stolen admin PAT yields one-call reconnaissance over every other credential's
name, owner and last-use time.

**Completeness is the security property, and pagination does not guarantee it.**
`ORDER BY` carries no tiebreaker (project-wide — `users.list_users` is the same), and
`api_tokens` has no index on `created_at`/`last_used_at` (only `uq_api_tokens_token_hash`
and partial `(user_id) WHERE revoked_at IS NULL`). Postgres DESC is NULLS FIRST, so
`sort=last_used_at_desc` leads with the dense tie block of never-used tokens; paging
through unordered ties can omit rows, which on an inventory means a live credential an
admin never sees. Also note `include_revoked=False` filters `revoked_at IS NULL` only —
**expired tokens still appear as "active"**.

Related: [[auth-credential-carrier-inventory]]
