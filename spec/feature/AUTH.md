# Authentication & User Identity

> This document specifies DataSpoke's user identity, authentication, and the
> semantics by which DataSpoke projects role and group membership onto DataHub
> corpusers.
>
> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).
> Route catalogue, JWT claim shape, middleware stack, and error codes in
> [API](../API.md).
> DataHub SDK patterns (corpuser/corpGroup CRUD, role assignment, GraphQL
> mutations) in [DATAHUB_INTEGRATION](../DATAHUB_INTEGRATION.md).
> DB schema for the `users` and `password_reset_tokens` tables in
> [BACKEND_SCHEMA](BACKEND_SCHEMA.md).
> Backend service modules in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Identity Model](#identity-model)
3. [Data Model](#data-model)
4. [Lifecycle](#lifecycle)
5. [DataHub Projection Semantics](#datahub-projection-semantics)
6. [Marker corpGroup](#marker-corpgroup)
7. [Privilege Model](#privilege-model)
8. [API Tokens](#api-tokens)
9. [Role Drift Reconciliation](#role-drift-reconciliation)
10. [Admin Surface](#admin-surface)
11. [Built-in Bootstrap Admin](#built-in-bootstrap-admin)
12. [Failure Modes](#failure-modes)
13. [Security Considerations](#security-considerations)
14. [Out of Scope](#out-of-scope)

---

## Overview

DataSpoke users authenticate to the DataSpoke API with email + password or Google
OAuth, receiving a short-lived JWT access token and a long-lived refresh token,
or via long-lived opaque API tokens minted under `/auth/api-tokens` for
non-interactive clients. User creation is entirely local: no DataHub call sits
on any signup path, so accounts can be created before DataHub is wired at all.
A person's DataHub `corpuser` is created by DataHub's own OIDC just-in-time
provisioning on their first DataHub login; DataSpoke *projects* role and
marker-group membership onto that corpuser once it exists, and only for users
who have linked a Google identity. Roles
(`Admin` / `Editor` / `Reader`) live in the DataSpoke `users.role` column as the
SSOT and propagate DataSpoke→DataHub via `batchAssignRole`; a nightly DAG
reconciles both projected facets.

The same Google OAuth client serves both DataSpoke (for API login) and DataHub
(for native UI access via OIDC), so a user authenticates with Google once per
session per system and is recognised by both via shared email.

---

## Identity Model

DataSpoke is the SSOT for both user identity and role. DataHub holds
propagated copies for its own UI-side authorization, but DataSpoke is
authoritative on what every DataSpoke user can do — both on the DataSpoke
API and (via propagation) inside the DataHub UI.

| Concern | Owner | Notes |
|---------|-------|-------|
| User identity — email, display name, password hash, Google `sub` | **DataSpoke** `users` table | DataSpoke is the auth front-door; only DataSpoke issues tokens. |
| User role (Admin / Editor / Reader) | **DataSpoke** `users.role` column | Authoritative across both DataSpoke API and DataHub-side gating. Default at registration: `Reader`. |
| DataHub corpuser entity + profile | **DataHub** | Created and refreshed by DataHub's OIDC JIT provisioning from Google claims on each DataHub login — see [§DataHub OIDC JIT provisioning](#datahub-oidc-jit-provisioning). DataSpoke writes no corpuser aspect. |
| Role + marker-group membership on the corpuser | **DataSpoke** projects onto DataHub | DataSpoke writes the role via `batchAssignRole` and membership via `addGroupMembers` so DataHub-UI authorization sees the same role. The projection is one-way (DataSpoke→DataHub) and applies only to corpusers that already exist, for users whose row carries a `google_sub` — see [§Identity-binding requirement](#identity-binding-requirement). A nightly DAG reconciles drift — see [§Role Drift Reconciliation](#role-drift-reconciliation). |
| Provenance marker (which corpusers were created by DataSpoke) | DataHub corpGroup | A single named corpGroup (default `dataspoke-users`) lists every DataSpoke-managed corpuser. Lets ops distinguish DataSpoke users from natively-created or other-SSO users in DataHub. |

DataSpoke never writes the DataHub `corpUserCredentials` aspect. The DataHub
corpuser carries a profile sourced from Google claims plus the DataSpoke-projected
role and group membership, but has no usable native password. DataHub UI access
uses Google OIDC; DataHub native login with a DataSpoke-set password is not a
supported path.

---

## Data Model

DataSpoke stores:

- **`users`** — one row per DataSpoke-managed identity. Columns:
  `id (uuid pk)`, `email (citext unique)`, `name`, `password_hash (nullable)`,
  `google_sub (nullable unique)`, `role (enum Admin | Editor | Reader)`,
  `created_at`, `updated_at`. At least one of `password_hash` and `google_sub`
  must be set. User deletion is hard delete — no `deleted_at` column.
- **`api_tokens`** — long-lived personal access tokens minted by users for
  non-interactive clients. Columns: `id (uuid pk)`, `user_id (fk)`, `name`,
  `token_hash (sha256, unique)`, `role_snapshot`, `created_at`,
  `last_used_at`, `expires_at (nullable)`, `revoked_at (nullable)`.
- **`password_reset_tokens`** — single-use reset tokens. Columns:
  `token_hash (sha256, pk)`, `user_id (fk)`, `expires_at`, `used_at`.

Full column types, indexes, and constraints in
[BACKEND_SCHEMA](BACKEND_SCHEMA.md). Role is now a first-class column on
`users` — see [Privilege Model](#privilege-model) for how it gates DataSpoke
routes.

---

## Lifecycle

### Email + password registration

`POST /auth/register` is open self-service. The request body carries
`{email, name, password}`; password must be at least 10 characters (no other
spec-level complexity rules). On success, DataSpoke creates the local `users`
row with a bcrypt password hash and `role = 'Reader'`, and returns an access +
refresh token pair so the user is logged in immediately. Registration is a
purely local transaction — it makes no DataHub call and therefore succeeds
whether or not the DataHub peripheral is configured or reachable.
Email is not verified at registration — the typosquatting risk
is accepted; see [Security Considerations](#security-considerations) for the
mitigation rationale.

### Google OAuth registration & login

`GET /auth/google/login` redirects the browser to Google. State and nonce
are stored in a signed Starlette session cookie (HMAC-signed via
`DATASPOKE_OAUTH_STATE_SECRET`) — authlib handles the round-trip end-to-end.
`GET /auth/google/callback` validates state + nonce against the session,
exchanges the authorisation code for an ID token, and resolves the user:

| Google `sub` known? | Google `email` matches existing row? | Action |
|---|---|---|
| Yes | — | Log in. Refresh display name from the Google profile onto the DataSpoke row. |
| No | Yes | Link `google_sub` onto the existing row. Log in. |
| No | No | Create a fresh `users` row with `password_hash=null` and `role = 'Reader'`. Log in. |

Linking preserves password access — a user who registered with email + password
can later add Google without losing the ability to log in with the password.

### Login

`POST /auth/token` with `{email, password}` issues an access JWT (15 min
lifetime, in response body) and a refresh JWT (7 d lifetime, as `HttpOnly`
cookie). The access JWT carries identity only — `sub`, `email`, `exp`, `iat`.
The JWT does not encode role (see [Privilege Model](#privilege-model)); role
is read per-request from `users.role`.

### Refresh & revoke

`POST /auth/token/refresh` validates the refresh-cookie JWT, checks the Redis
revocation list, and issues a fresh access token with the same identity claims.
`POST /auth/token/revoke` records the refresh token's hash in Redis under
`revoked_refresh:{sha256[:16]}` with TTL equal to the token's remaining lifetime;
both flows fail-closed on Redis unreachability (`503 STORAGE_UNAVAILABLE`).
On that 503 path revoke **retains the refresh cookie**: the token is still live
server-side, and clearing the cookie would signal a revocation that did not
occur — a fail-open dressed as an error.

Neither route takes a bearer credential; the refresh cookie is the credential.
They differ on a missing or unusable one. Refresh requires a live refresh token
and returns `401 UNAUTHORIZED` without one. Revoke is credential-optional and
idempotent — a missing, undecodable, wrong-signature, expired, or
non-`type=refresh` cookie is a no-op on the revocation store: the cookie is
cleared and the call returns `204`. There is no live token to revoke, and per
RFC 7009 §2.2 revocation reports success whether the token was revoked or was
already invalid. Logout therefore never fails on account of the cookie it was
handed, only on the store being unreachable.

### Same-site requirement for cookie-based session

The refresh JWT is an `HttpOnly`, `SameSite=Lax` cookie scoped to the API host;
Lax cookies are sent only on same-site requests, so the browser UI and the API
must share a registrable domain for the cookie to flow on `/auth/token/refresh`.
Password login is unaffected on first sign-in (its access token is returned in
the response body), but **Google/OIDC login establishes the session solely via
this cookie**: the callback sets the cookie and 302-redirects to
`oauth_post_login_redirect` (the UI origin, not an API-relative path), and the UI
then calls `/auth/token/refresh` to obtain an access token. That refresh call
must be same-site for the cookie to be sent.

In prod the UI and API share a domain, so this holds. In dev it holds for the
in-cluster frontend (`app.<INGRESS_IP>.nip.io`, same site as the API at
`api.<INGRESS_IP>.nip.io`) but not for host `pnpm dev` on `localhost:3000`, which
is cross-site to the nip.io API — so OIDC login completes only from the in-cluster
frontend. `SameSite=None` would lift this but requires HTTPS, which the dev stack
does not serve. Host `pnpm dev` should use password login.

### Profile read & update

`GET /auth/me` returns the caller's `users` row (without `password_hash`),
including `users.role`. `PATCH /auth/me` accepts `{name?, password?}`: both
fields write to the DataSpoke `users` row only and make no DataHub call.
Display name is not projected — the DataHub-side profile is DataHub's own,
refreshed from Google claims at each OIDC login (see
[§DataHub OIDC JIT provisioning](#datahub-oidc-jit-provisioning)), so a
DataSpoke display name and a DataHub display name may legitimately differ.

### Password reset

`POST /auth/password/reset/request` accepts `{email}`. If the email exists,
DataSpoke writes a single-use token row (SHA-256 hash of a random opaque
token, 15-min TTL) and sends an email containing the raw token via the SMTP
peripheral. The SMTP peripheral follows the same split-storage pattern as
DataHub and Langfuse: non-secret fields (`host`, `port`, `username`,
`from_address`, `use_tls`) in `peripheral_config`, the `password` field in
the dedicated K8s Secret `dataspoke-smtp-secret`. If the peripheral is not
configured (no row in `peripheral_config`, or the Secret unset), the request
returns `503 PERIPHERAL_NOT_CONFIGURED`. The response is the same shape for
known and unknown emails (no account-enumeration leak).

`POST /auth/password/reset/confirm` consumes `{token, new_password}`,
validates the token (matches a row, not expired, `used_at is null`), writes
the new bcrypt hash, and marks the token used. No DataHub write occurs.

### Deletion

`DELETE /admin/users/{id}` runs the [projection retraction
sequence](#projection-retraction-sequence). The DataSpoke `users` row is removed
physically (hard delete) and the DataHub corpuser is hard-deleted via the
`acryl-datahub` SDK's `hard_delete_entity` (which also removes references to
the corpuser across the DataHub metadata graph — group memberships,
ownership, role assignments). Email is immediately reusable for a fresh
registration after deletion. Audit trails are not maintained by DataSpoke;
organisations that need historical records take them from logs.

A still-valid access token whose subject was deleted fails with
`401 UNAUTHORIZED`, and a `/auth/token/refresh` attempt carrying the deleted
user's refresh cookie also fails with `401 UNAUTHORIZED` (the cookie is revoked
before the user lookup, so the failure is fail-closed). A deleted subject is an
authentication failure — the client must re-authenticate, not an authorization
failure — so the frontend clears the session and redirects to `/login`.

---

## DataHub Projection Semantics

DataHub owns the corpuser entity; DataSpoke projects role and marker-group
membership onto it. Aspect names and SDK patterns are defined in
[DATAHUB_INTEGRATION §User & Role
Management](../DATAHUB_INTEGRATION.md).

### DataHub OIDC JIT provisioning

DataHub is configured for Google OIDC SSO against the **same Google client**
DataSpoke uses (chart wiring in
[HELM_CHART §DataHub OIDC](HELM_CHART.md)). On a person's first DataHub login,
DataHub just-in-time provisions their `corpuser` entity from the ID-token
claims and refreshes the profile on every subsequent login. This is the only
mechanism that creates a corpuser for a DataSpoke user.

**Required DataHub OIDC settings.** DataHub derives the corpuser id from an
ID-token claim named by `user_name_claim`, then applies `user_name_claim_regex`
to it. Two settings must hold for the projection to work at all:

| Setting | Required value | Why |
|---------|----------------|-----|
| `user_name_claim` | `email` | Selects the email claim as the corpuser id source. |
| `user_name_claim_regex` | `(.*)` | Keeps the whole address. DataHub's default `([^@]+)` strips the domain and yields `urn:li:corpuser:bob`, which no DataSpoke row addresses. |

This is a **hard prerequisite**, not a tuning preference: with the default
regex the JIT-provisioned URN and the DataSpoke-derived URN never coincide, so
every projection silently targets a non-existent entity and every user is
reported `skipped_unprovisioned` forever. Operators wiring their own DataHub
must set both. Settings reference:
[DataHub OIDC configuration](https://docs.datahub.com/docs/authentication/guides/sso/configure-oidc-react).

With both set, the JIT-provisioned URN is exactly `urn:li:corpuser:<email>`
for the same address held in DataSpoke `users.email`, so DataSpoke addresses a
user's corpuser from its own row alone — no lookup, no identity mapping table.

The consequence is a **provisioning lag**: a DataSpoke user who has never
signed into DataHub has no corpuser, and nothing to project onto. See
[§Projection contract](#projection-contract).

### URN conventions

- corpuser URN: `urn:li:corpuser:<email>`, as provisioned by DataHub's OIDC JIT
  under the settings above. **The email is lowercased before URN derivation.**
  DataSpoke `users.email` is `CITEXT` — case-insensitive on compare, but
  case-preserving on storage — while the corpuser URN is case-sensitive. User
  creation therefore normalises the address to lowercase on write, so the two
  sides agree by construction; URN derivation lowercases again as a second line
  of defence, so a row stored in any case still derives
  `urn:li:corpuser:bob@example.com` to meet the URN DataHub provisions.
- corpGroup URN: `urn:li:corpGroup:<name>`, where `<name>` is the value of
  `/admin/conf.auth_datahub_corp_group` (default `dataspoke-users`) — the
  single marker group.

### Aspects DataSpoke writes

| Entity | Aspect | When |
|--------|--------|------|
| corpGroup | `corpGroupInfo` + `Status(removed=false)` | On marker-group lazy-create. |

That single row is the whole of it: DataSpoke writes **no corpuser aspect at
all**. `corpUserInfo` belongs to DataHub's OIDC JIT provisioning, and
`corpUserCredentials` is deliberately unused (see
[§Identity Model](#identity-model)). The two projected facets reach DataHub
through GraphQL mutations rather than aspect emission — role via
`batchAssignRole`, marker-group membership via `addGroupMembers`.

Role propagation uses the GraphQL `batchAssignRole` mutation, called after a
DataSpoke-side `users.role` write via `PATCH /admin/users/{id}/role` and by the
nightly reconciliation pass. Role *read* is **not on the request hot path** — the per-request
privilege check reads from DataSpoke `users.role` directly. The DataHub-side
read is used only by the nightly `auth-role-sync-daily` DAG to detect drift,
and reads the `RoleMembership` aspect directly (atomic single-role per
DataHub `RoleService`) rather than the `IsMemberOfRole` GraphQL relationship
index, which lags MCL→ES indexing. Group membership writes use `addGroupMembers` /
`removeGroupMembers`. User deletion uses the SDK's `hard_delete_entity` on
the corpuser URN (no aspect write; the entity and all its incoming /
outgoing references are removed).

### Projection contract

User creation is local-only. Neither `POST /auth/register`, nor the
Google-OAuth new-user branch, nor `POST /internal/admin/bootstrap` makes a
DataHub call — each inserts the DataSpoke `users` row (default `role = 'Reader'`,
`Admin` for bootstrap) and issues tokens. DataSpoke never creates a corpuser.

Two paths project DataSpoke-owned state onto a corpuser DataHub has already
provisioned:

| Path | Trigger | Facets projected |
|------|---------|------------------|
| Write-through | `PATCH /admin/users/{id}/role` | Role, via `batchAssignRole`, after the `users.role` write commits. |
| Reconciliation | Nightly `auth-role-sync-daily` | Role **and** marker-group membership, for every eligible row in `users` — see [§Role Drift Reconciliation](#role-drift-reconciliation). |

Both paths are idempotent and best-effort: a DataHub failure never rolls back
or blocks the DataSpoke-side write, because DataSpoke is the SSOT for both
identity and role and the reconciliation pass converges the projection on its
next run. There is no compensating delete anywhere on the creation paths, and
no user-facing error code for a failed projection.

#### Identity-binding requirement

**Both paths project only onto users whose row has `google_sub IS NOT NULL`.**
A row created by password registration alone is never projected, on either
path.

The reason is that `urn:li:corpuser:<email>` addresses whoever DataHub's OIDC
JIT provisioned at that address — a real person who proved ownership of it to
Google. DataSpoke has no such proof for a password-registered row: email is
not verified at registration, so the row asserts an address it may not own.
Projecting from an unverified row would let one person's DataSpoke role land
on a different person's DataHub identity. A non-null `google_sub` is the
provider-verified binding between the two, and it is the precondition for
writing anything.

Accepted trade-off, stated plainly: a password-only DataSpoke account receives
no DataHub projection even when that person does use DataHub under the same
address. Their DataHub role stays whatever DataHub itself assigns. They
establish the binding by signing into DataSpoke with Google once — linking
`google_sub` onto the existing row (see [§Google OAuth registration &
login](#google-oauth-registration--login)) — after which the next
reconciliation pass projects both facets. The bootstrap admin
(`dataspoke@dataspoke.local`) has no Google identity and is therefore never
projected, which is consistent with its corpuser being unprovisionable in the
first place.

Reconciliation counts unbound rows as `skipped_unbound`, distinct from
`skipped_unprovisioned` (bound, but no corpuser exists yet).

The projection therefore lags two independent events: the Google link and the
first DataHub login. A user missing either is skipped into the corresponding
bucket and repaired on the first pass after both hold.

### Projection retraction sequence

1. Hard-delete the DataSpoke `users` row.
2. Hard-delete the DataHub corpuser via `hard_delete_entity`.

Group memberships, role assignments, and ownership references to the corpuser
are removed automatically by DataHub when the entity is hard-deleted, so no
separate group-removal step is required. If step 2 fails after step 1 succeeded,
the DataSpoke row is gone but the DataHub corpuser lingers as an orphan —
operators clean it up via DataHub directly (the orphan is visible in DataHub
listings). The order is chosen so the user is immediately unable to log into
DataSpoke even if the DataHub side fails.

Deletion is the **only** path that retracts a projection. Reconciliation
iterates the DataSpoke `users` table, so a deleted user is invisible to it and
its stale role and marker-group membership would otherwise persist indefinitely,
leaving DataSpoke-granted DataHub privileges attached to an account DataSpoke no
longer knows. Step 2 hard-deletes an entity that DataHub's JIT provisioning
created — an accepted, deliberate asymmetry, taken because retracting the
privilege matters more than leaving DataHub's entity untouched. The person can
re-provision their own corpuser simply by logging into DataHub again, at which
point it carries no DataSpoke projection.

Convergence is therefore scoped to users that exist: for every row in `users`,
the nightly pass drives the DataHub side to match. Stale projections belonging
to *deleted* users are not self-healing — if step 2 fails, an operator clears
the orphan in DataHub.

---

## Marker corpGroup

The marker corpGroup is the provenance signal that distinguishes
DataSpoke-managed corpusers from natively-created users, OIDC-only users, or
service accounts. It has no privilege effect on its own — it is a label.
The DataHub `corpGroup` entity used here is a metadata-graph artifact and does
not factor into DataSpoke API authorization, which is method × role.

| Property | Value |
|----------|-------|
| Group URN | `urn:li:corpGroup:<admin/conf.auth_datahub_corp_group>` |
| Default name | `dataspoke-users` |
| Configured via | `PATCH /admin/conf` (`auth_datahub_corp_group` field). |
| Created via | Self-healing: every reconciliation pass asserts the group once, unconditionally, before its per-user loop. Both `Status(removed=false)` and `corpGroupInfo` are asserted together (idempotent overwrite — defensive against the DataHub indexing race in which a previous failed attempt left only one of the two aspects committed). Ensuring the group **must precede** `addGroupMembers`, which rejects an unresolvable group URN. The `displayName` is reset to the group name and `members`/`admins`/sub-`groups` are reset to empty arrays on every touch — real membership lives on each user's `nativeGroupMembership` aspect — so the marker group **must not be used as a privilege carrier**. Operators wanting a different display name update `auth_datahub_corp_group` via `/admin/conf` instead of editing it on DataHub. |
| Rename behaviour | Changing `auth_datahub_corp_group` does not migrate existing memberships. The next reconciliation pass creates the new group and adds every provisioned managed user to it; those users also remain in the old group, which becomes orphaned from DataSpoke's perspective but stays valid on DataHub. Operators avoid this by renaming the corpGroup on DataHub first, then updating the conf field. |

---

## Privilege Model

The caller's role (`Admin` / `Editor` / `Reader`) is read from DataSpoke
`users.role` on each request — no GraphQL call to DataHub on the hot path.
For API-token-carried requests, the effective role is the **intersection**
of the token's `role_snapshot` and the owner's current `users.role` (see
[§API Tokens](#api-tokens)).

The privilege model is two-axis: URI prefix × HTTP method.

| URI prefix | Reader | Editor | Admin |
|------------|--------|--------|-------|
| `/auth/*` (self-scoped) | ✓ all methods | ✓ all methods | ✓ all methods |
| `/spoke/*` | ✓ `GET` / `HEAD` / `OPTIONS` only | ✓ all methods | ✓ all methods |
| `/admin/*` | ✗ | ✗ | ✓ all methods |

Failure responses:

| Condition | Response |
|---|---|
| Reader attempting a write method on `/spoke/*` | `403 READ_ONLY_ROLE` |
| Reader on `GET /spoke/ingestion/secrets` (Editor+ exception — see below) | `403 READ_ONLY_ROLE` |
| Editor or Reader attempting `/admin/*` | `403 FORBIDDEN` |
| Role row missing (defensive — should not occur post-registration) | `403 FORBIDDEN` |

**Editor+ read exception:** `GET /spoke/ingestion/secrets` requires Editor or Admin even though
it is a GET — enumerating which source-credential references exist is author/operator tooling
(see [SECRET_RESOLUTION.md §Reference discovery](SECRET_RESOLUTION.md#reference-discovery-list-flow)).
It is the only read route that deviates from the Reader-GET rule above.

Role changes take effect on the **next request**, both for JWT-authenticated
sessions and for API-token requests (the intersection check reads
`users.role` fresh on every call). There is no issue-time cache or
JWT-baked claim to wait out — instant demotion.

`/auth/*` routes are exempt from the method gate: any role can `POST /auth/token/refresh`,
`PATCH /auth/me` (own name/password), `POST /auth/api-tokens` (own tokens),
etc. These are self-scoped writes.

### Self-demotion footgun

`PATCH /admin/users/{id}/role` permits a sole admin to demote themselves.
Once demoted, they cannot promote themselves back — only another Admin can.
The spec does not gate self-demotion (it's standard role-system behaviour);
operators are advised to keep at least two Admin users in production.

---

## API Tokens

Long-lived personal access tokens (PATs) for non-interactive clients (CI
jobs, AI agents, third-party integrations). Self-service: every authenticated
user mints, lists, and revokes their own tokens under `/auth/api-tokens`.
Admins can list and revoke any user's tokens via
`/admin/users/{id}/api-tokens` (for incident response). Admins cannot mint
tokens on behalf of other users — only the owner can mint.

### Token format and storage

- Opaque random tokens of the form `dsk_<32 url-safe random bytes>`
  (generated via `secrets.token_urlsafe(32)`). The `dsk_` prefix is for
  grep-friendly leak detection in logs and source control.
- Only the SHA-256 hash of the token is stored in the `api_tokens` table
  (column `token_hash`). The raw token is returned **once** in the
  `POST /auth/api-tokens` response body and never retrievable again.
- Caps: at most 10 active (`revoked_at IS NULL`) tokens per user. Mint
  beyond cap returns `409 TOKEN_LIMIT_EXCEEDED`.

### Token carriage

API tokens are carried in the same `Authorization: Bearer <token>` header as
JWTs. The auth dependency routes on the token shape: a value with the `dsk_`
prefix goes straight to opaque-token lookup against `api_tokens.token_hash`;
any other value is JWT-decoded. Either form populates the same request-context
identity. There is no separate header.

### Effective privilege — intersection

On each request authenticated by an API token, the middleware computes:

```
effective_role = min(token.role_snapshot, owner.users.role)
```

where the ordering is `Admin > Editor > Reader`. This means:

- Demoting a user **immediately** downgrades all their existing tokens —
  no separate revoke step needed for role-change incident response.
- Promoting a user does **not** automatically elevate their existing
  tokens; the user must mint a new token to gain the higher privilege via
  PAT. (Mint time is fast; no migration concern.)
- A token whose hash matches no stored token fails authentication with
  `401 INVALID_API_TOKEN`.
- A revoked token (`revoked_at IS NOT NULL`) fails authentication with
  `401 TOKEN_REVOKED` regardless of the owner's current role.
- An expired token (`expires_at` in the past) fails with `401 TOKEN_EXPIRED`.

### Audit and `last_used_at`

Every successful API-token authentication updates `api_tokens.last_used_at`.
The update is throttled to per-minute granularity (the middleware checks
`now - last_used_at > 60s` before issuing the `UPDATE`) so a high-frequency
client doesn't flood the DB. Stale updates from concurrent requests are
acceptable — `last_used_at` is for human inspection, not race-sensitive
billing.

### Lifecycle endpoints

The API contract lives in [API §Auth](../API.md#auth) and
[API §Admin](../API.md#admin-admin). Summary:

| Endpoint | Purpose |
|----------|---------|
| `GET /auth/api-tokens` | List own tokens (returns `{tokens: [{id, name, role_snapshot, created_at, last_used_at, expires_at}], total}` — never the raw token) |
| `POST /auth/api-tokens` | Mint a new token (body `{name, expires_at?}`); response includes the raw token in `{token: "dsk_..."}` — only time it is returned plain |
| `DELETE /auth/api-tokens/{id}` | Revoke an own token (sets `revoked_at = now()`) |
| `GET /admin/users/{id}/api-tokens` | List a user's tokens (admin) |
| `DELETE /admin/users/{id}/api-tokens/{token_id}` | Revoke a user's token (admin; incident response) |

---

## Role Drift Reconciliation

DataSpoke `users.role` is the SSOT and the marker group is DataSpoke's
provenance label, so both projected facets are DataSpoke's to assert. They can
diverge — an operator changes a role or edits group membership directly in the
DataHub UI, a write-through call failed, or the corpuser simply did not exist
when the projection was first attempted.

The nightly Airflow DAG `auth-role-sync-daily` is the self-healing pass. It
asserts the marker corpGroup **once, unconditionally, before the loop** — the
group must resolve before any `addGroupMembers` call in the pass — then for
each row in `users` (ordered by `id`):

1. **Binding gate** — skip rows with `google_sub IS NULL`, counting them
   `skipped_unbound`. Only a provider-verified identity binding authorises a
   write against `urn:li:corpuser:<email>` (see [§Identity-binding
   requirement](#identity-binding-requirement)).
2. Derive the corpuser URN from the lowercased `users.email` and **probe for
   the corpuser's existence before any mutation**. This guard is load-bearing:
   DataHub's `RoleService` returns early when the actor does not exist while
   the GraphQL mutation still reports success, so a pass that trusted the
   mutation result would report repairs for users it never touched. Users
   without a corpuser count as `skipped_unprovisioned` and are left alone.
3. **Role facet** — read the corpuser's `RoleMembership` aspect directly
   (atomic single-role per DataHub `RoleService`). The `IsMemberOfRole`
   GraphQL relationship index is not used: it lags MCL→ES indexing and
   transiently shows roles already overwritten in the aspect. On divergence
   from `users.role`, re-assert via `batchAssignRole` — **DataSpoke wins**.
4. **Group facet** — read the corpuser's `nativeGroupMembership` aspect. If
   the marker group URN is absent, add it via `addGroupMembers`.
5. If either facet was repaired, emit one `AUTH.ROLE_SYNC_FIXED` event whose
   `detail` records which facet(s) were repaired and, for the role facet, the
   observed and authoritative roles.

The pass returns
`{checked, fixed, skipped_unbound, skipped_unprovisioned, errors}`. All
counters are per *user*, not per facet:

| Counter | Meaning |
|---------|---------|
| `checked` | Rows examined. |
| `fixed` | Users where at least one facet was repaired. The per-facet breakdown lives in the event `detail`. |
| `skipped_unbound` | Rows with `google_sub IS NULL` — no identity binding, nothing attempted. |
| `skipped_unprovisioned` | Bound rows whose corpuser does not exist. |
| `errors` | Users for whom at least one facet could not be reconciled. The next nightly run retries. |

The two facets are read and repaired independently, so a user can have one
facet repaired and the other fail. Such a user counts in **both** `fixed` and
`errors`. The buckets are therefore not a partition and need not sum to
`checked`.

The auto-fix is intentional: DataSpoke is the SSOT, so any DataHub-side drift
is by definition a mistake to be corrected. The pass iterates only rows in
DataSpoke's `users` table — DataHub-only corpusers (e.g. a super-admin not
managed by DataSpoke) are out of scope. Operators who need a DataHub-only role
assignment keep that corpuser out of the DataSpoke `users` table entirely.

Convergence holds for users that exist in `users`. It does not extend to users
already deleted from DataSpoke — see
[§Projection retraction sequence](#projection-retraction-sequence).

The per-user fan-out is bounded (a small constant number of round trips per
managed corpuser per day). For large deployments this could be batched via
`scrollAcrossEntities`; baseline keeps the simple form.

The DAG body is a thin HttpOperator call to
`POST /internal/activities/auth/role-sync` (gated by `X-Internal-Token`);
that internal-activity endpoint owns the loop, role read, and
`AUTH.ROLE_SYNC_FIXED` event emission. Per-activity route shapes are not
catalogued in [API](../API.md) — see [API §Internal Activities](../API.md#internal-activities-internalactivities).

---

## Admin Surface

The admin route catalogue is defined in [API §Admin Routes](../API.md). The
surfaces relevant to user identity:

| Route | Purpose |
|-------|---------|
| `GET /admin/users` | List DataSpoke users — `users.role` is returned per row (DB column, no GraphQL call). |
| `PATCH /admin/users/{id}` | Update display name (email is immutable post-creation because the DataHub corpuser URN is immutable). |
| `PATCH /admin/users/{id}/role` | Update `users.role` (Admin / Editor / Reader) and, when the row carries a `google_sub`, propagate to DataHub via `batchAssignRole`. DataSpoke is SSOT; the DataHub-side projection is one-way. |
| `DELETE /admin/users/{id}` | Hard delete ([projection retraction sequence](#projection-retraction-sequence)). |
| `PATCH /admin/conf` | Includes `auth_datahub_corp_group` (string, default `dataspoke-users`) — names the marker corpGroup; asserted once per reconciliation pass. |

`/admin/*` routes require `users.role = 'Admin'` — checked per-request per
[Privilege Model](#privilege-model). The JWT carries no admin claim.

---

## Built-in Bootstrap Admin

Every fresh install needs an Admin user to drive `/admin/*` and start
registering peripherals; spec'ing one well-known account closes the
chicken-and-egg gap. `POST /internal/admin/bootstrap` (gated by
`X-Internal-Token`) seeds the row idempotently: if any user with
`role = 'Admin'` already exists it returns `{created: false}` and changes
nothing.

| Property | Value |
|----------|-------|
| Login identifier | `dataspoke@dataspoke.local` — a DataSpoke-only address with no Google identity behind it. The row carries no `google_sub`, so the reconciliation pass reports it as `skipped_unbound` at the binding gate; no corpuser is provisionable for it in any case |
| Display name | `DataSpoke Admin` |
| Initial password | `dataspoke` |
| Role | `Admin` |
| DataHub interaction | None. Bootstrap writes only the local `users` row, so it requires no peripheral configuration and succeeds on a fresh install before DataHub is wired. |

Both profiles run `helm-charts/bin/post-install/seed-admin-user.sh` after
the API pod is Ready, so the default admin exists as soon as the install
completes; `--skip-seed` suppresses it, leaving the operator to run the
script (or call `/internal/admin/bootstrap` directly) themselves. Because
the default credentials are published in this repository, rotating the
password via `PATCH /auth/me` is a required post-install step — the
install script logs a warning that explicitly says so. The bootstrap admin is otherwise a
normal Admin user: it can be hard-deleted via `DELETE /admin/users/{id}`
once another Admin exists, and the bootstrap endpoint will recreate it
on the next install only if zero Admin rows remain.

---

## Failure Modes

| Failure point | Behaviour | User-visible outcome | Operator action |
|---|---|---|---|
| DataHub unreachable or unconfigured during any user-creation path (`POST /auth/register`, Google-OAuth new user, `POST /internal/admin/bootstrap`) | No DataHub call is attempted; the transaction is purely local. | Creation succeeds and the user is logged in. | None. |
| A DataSpoke user has never logged into DataHub, so no corpuser exists | The reconciliation pass's existence probe skips them without mutating; counted `skipped_unprovisioned`. | None — the user's DataSpoke privileges are unaffected. | None; the projection lands on the first pass after their first DataHub login. |
| A `users` row has no `google_sub` (password-only account, or the bootstrap admin) | Neither projection path writes anything for it; the reconciliation pass counts it `skipped_unbound`. | None on the DataSpoke side; the user's DataHub role is whatever DataHub itself assigns. | None. The user binds the identity by signing into DataSpoke with Google once, after which the next pass projects both facets. |
| DataHub peripheral unconfigured when the nightly pass runs | The pass returns a no-op result rather than failing — operating before DataHub is wired is a supported steady state. | None. | None; the pass reconciles once the peripheral is configured. |
| Marker corpGroup missing on DataHub | The reconciliation pass creates it before projecting any membership. | None. | None. |
| Marker corpGroup assert fails at the start of a reconciliation pass | The pass aborts before its per-user loop rather than degrading. `addGroupMembers` rejects an unresolvable group URN, so a pass that continued would fail every group facet while reporting a clean run over the role facet. | Retryable error response; no counter result is returned. | None — Airflow retries the run. |
| SMTP peripheral missing during password-reset request | Request refuses; no DB write. | `503 PERIPHERAL_NOT_CONFIGURED`. | Admin configures `/admin/peripherals/smtp`. |
| SMTP configured but delivery fails (transport error, auth rejection, queue full) during password-reset request | Request refuses; no DB write — the token row is written only after `send_email` returns successfully. | `503 STORAGE_UNAVAILABLE` with a static message; the underlying SMTP error is logged but not echoed to the client. | Inspect API logs for the upstream cause; fix the SMTP path and retry. |
| Redis unreachable during refresh or revoke | Refresh/revoke fail-closed. | `503 STORAGE_UNAVAILABLE`. | Restore Redis. |
| Google OAuth state mismatch on callback | Callback aborts before token issuance. | `400 OAUTH_STATE_MISMATCH`. | User retries the OAuth flow. |
| Google OAuth callback receives ID token with `email_verified=false` | Callback rejects the token; no user is created or logged in. | `400 OAUTH_EMAIL_NOT_VERIFIED`. | User verifies their Google account email and retries. |
| Role change via `/admin/users/{id}/role`: DataSpoke write succeeds, DataHub propagation fails | The new role takes effect immediately on the DataSpoke API; DataHub-side stays stale until the nightly reconciliation DAG re-asserts. | The admin call returns `200` (DataSpoke side succeeded); a warning log records the propagation failure. | None — DAG handles. Manual recovery: re-PATCH the role. |
| Nightly reconciliation finds a divergence on either facet | The pass re-asserts `users.role` and/or marker-group membership to DataHub. | Operator visible via the `AUTH.ROLE_SYNC_FIXED` event row, whose `detail` names the repaired facet(s). | None unless the divergence was intentional (DataHub-only super-admin); in that case, keep that corpuser out of the DataSpoke `users` table. |
| API token revoked while in use | Next request with the token fails. | `401 TOKEN_REVOKED`. | None — expected behaviour. |
| API token whose owner was demoted | Effective privilege drops immediately to `min(snapshot, current role)`. | Write attempts return `403 READ_ONLY_ROLE`. | None — expected behaviour. |
| DataHub hard-delete fails after DataSpoke `users` row was deleted | Orphan corpuser remains in DataHub, carrying the last DataSpoke-projected role and marker-group membership. The user cannot log into DataSpoke, and reconciliation cannot retract the projection because the `users` row is gone. | `200 OK` to the admin caller (DataSpoke side succeeded). | Operator hard-deletes the orphan corpuser via DataHub directly. |

---

## Security Considerations

### Email verification omitted by design

Self-registration trusts the supplied email without a verification round-trip.
This admits a typosquatting class of abuse — a registrant could claim
`ceo@example.com` and hold a DataSpoke account under that address. The blast
radius is bounded to DataSpoke:

- **The abuse cannot reach DataHub.** This is the load-bearing mitigation, and
  it rests on the [identity-binding requirement](#identity-binding-requirement):
  a password-registered row has no `google_sub`, so neither projection path
  ever writes against `urn:li:corpuser:ceo@example.com`. The containment does
  not depend on the corpuser being absent — the real `ceo@example.com` may well
  sign into DataHub and have DataHub JIT-provision that corpuser, at which
  point it exists and belongs to them. The squatter's row still never
  addresses it, because the squatter cannot produce a Google identity for that
  address and therefore cannot bind their row to it.
- `POST /auth/register` is rate-limited (per the API rate-limit middleware) to
  raise the cost of mass registration.
- Default role is Reader. A typosquatted account cannot edit metadata or
  manage policies without an admin explicitly promoting it.
- Admin hard-delete removes the DataSpoke row and frees the email for
  re-registration by the legitimate owner. **Use the delete path with care
  here:** it also hard-deletes any corpuser at that URN, and under JIT
  provisioning that entity belongs to the *real* person, not the squatter —
  destroying it takes their ownership entries, role assignment, and group
  memberships across the DataHub metadata graph with it. Deleting a squatted
  row after the genuine owner has started using DataHub damages the victim,
  not the impostor. Operators who need only to free the email should confirm
  the corpuser's provenance in DataHub first.

If an organisation needs strict email ownership verification, it adds a
verification step in a fork (see [Out of Scope](#out-of-scope)).

### OAuth flow hardening

State and nonce are stored in the signed Starlette session cookie (HMAC
key `DATASPOKE_OAUTH_STATE_SECRET`); authlib generates fresh random
values on every `/auth/google/login` and validates them on callback.
Mismatches return `400 OAUTH_STATE_MISMATCH` without attempting token
exchange. The callback rejects ID tokens with `email_verified=false`
(`400 OAUTH_EMAIL_NOT_VERIFIED`) — unverified Google emails cannot
resolve to a DataSpoke account. If the credentials or the session
secret are unset, `/auth/google/{login,callback}` returns
`503 OAUTH_NOT_CONFIGURED`.

### Token-type confusion rejected

The access-token decoder rejects a JWT carrying `type = "refresh"` (→ `401`),
so a refresh token cannot be replayed on the `Authorization: Bearer` header to
authenticate a request. Symmetrically, the refresh endpoint accepts only
`type = "refresh"` JWTs (an access token presented there fails with
`INVALID_REFRESH_TOKEN`). Each token type is honoured only on its own path.

### Password storage

Passwords are stored as bcrypt hashes via `passlib`. Hash parameters
(cost factor) are defined in [BACKEND §Auth Service](BACKEND.md). The
DataSpoke `users.password_hash` column is the only password store —
DataHub's `corpUserCredentials` is intentionally unused.

### Password-reset token storage

The `password_reset_tokens` table stores the SHA-256 hash of the raw token,
never the raw token itself. The raw token exists only in the email body and
in the user's clipboard. A leaked DB therefore reveals no usable reset tokens.

### Cookie flags

The refresh-token cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` only
when `DATASPOKE_COOKIE_SECURE=true` (production default in chart values).

---

## Out of Scope

The following are explicitly not part of the baseline auth surface:

- **DataHub native login with a DataSpoke-set password.** DataHub users access
  the DataHub UI via Google OIDC; the DataHub `corpUserCredentials` aspect is
  never written. Organisations needing DataHub-native password login configure
  it directly on DataHub outside DataSpoke.
- **Bidirectional password sync.** Passwords change only in DataSpoke and
  remain there.
- **Email ownership verification.** No magic-link or code-based verification
  step. See [Security Considerations](#security-considerations).
- **LDAP, SAML, additional OIDC providers.** Only Google OAuth is supported in
  the baseline; additional IdPs are organisation-specific extensions.
- **Per-user impersonation against DataHub.** All DataSpoke→DataHub writes
  (including those triggered by a DataSpoke user — review approvals, role
  changes, role and group projection writes) use the single pre-configured admin-level
  service token from `dataspoke-datahub-secret`. DataHub's aspect-level
  audit attribution therefore points to the service-token's corpuser, not
  the real DataSpoke user. User-level audit lives in the DataSpoke `events`
  table. See [DATAHUB_INTEGRATION §Service Credential Model](../DATAHUB_INTEGRATION.md#service-credential-model).
- **Per-feature fine-grained authorisation.** The baseline gates routes by
  prefix (`/spoke/*`, `/admin/*`) and HTTP method × role;
  per-resource ACLs on individual datasets, runs, or reviews are out of
  scope and live in DataHub policies.
