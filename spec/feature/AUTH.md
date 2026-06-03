# Authentication & User Identity

> This document specifies DataSpoke's user identity, authentication, and the
> sync semantics that mirror DataSpoke users into DataHub.
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
5. [DataHub Mirror Semantics](#datahub-mirror-semantics)
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
non-interactive clients. Every DataSpoke user is mirrored into DataHub as a
`corpuser` so DataHub can attach ownership, group membership, and a propagated
role to that identity. Roles (`Admin` / `Editor` / `Reader`) live in the
DataSpoke `users.role` column as the SSOT and propagate DataSpoke→DataHub via
`batchAssignRole`; a nightly DAG reconciles drift.

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
| DataHub corpuser shadow + role mirror | DataSpoke writes; DataHub holds | DataSpoke emits `corpUserInfo` and writes the role via `batchAssignRole` so DataHub-UI authorization sees the same role. The mirror is one-way (DataSpoke→DataHub); a nightly DAG reconciles drift — see [§Role Drift Reconciliation](#role-drift-reconciliation). |
| Provenance marker (which corpusers were created by DataSpoke) | DataHub corpGroup | A single named corpGroup (default `dataspoke-users`) lists every DataSpoke-managed corpuser. Lets ops distinguish DataSpoke users from natively-created or other-SSO users in DataHub. |

DataSpoke never writes the DataHub `corpUserCredentials` aspect. The DataHub
corpuser exists as a shadow record — it carries the user's display info, role,
and group membership, but has no usable native password. DataHub UI access uses
Google OIDC; DataHub native login with a DataSpoke-set password is not a
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
row with a bcrypt password hash, executes the [DataHub mirror create
sequence](#mirror-create-sequence) (corpuser → marker-group membership → Reader
role), and returns an access + refresh token pair so the user is logged in
immediately. Email is not verified at registration — the typosquatting risk
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
| Yes | — | Log in. Refresh display name from Google profile. |
| No | Yes | Link `google_sub` onto the existing row. Log in. |
| No | No | Create a fresh `users` row with `password_hash=null`. Run the DataHub mirror create sequence. Log in. |

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
including `users.role`. `PATCH /auth/me` accepts `{name?, password?}`:
name updates write to DataSpoke `users` and propagate the new display name to
DataHub via `corpUserInfo`; password updates rewrite `users.password_hash` only
and do not touch DataHub (consistent with the no-`corpUserCredentials`-writes
rule).

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

`DELETE /admin/users/{id}` runs the [DataHub mirror delete
sequence](#mirror-delete-sequence). The DataSpoke `users` row is removed
physically (hard delete) and the DataHub corpuser is hard-deleted via the
`acryl-datahub` SDK's `hard_delete_entity` (which also removes references to
the corpuser across the DataHub metadata graph — group memberships,
ownership, role assignments). Email is immediately reusable for a fresh
registration after deletion. Audit trails are not maintained by DataSpoke;
organisations that need historical records take them from logs.

---

## DataHub Mirror Semantics

DataSpoke mirrors every managed user into DataHub. Aspect names and SDK
patterns are defined in [DATAHUB_INTEGRATION §User & Role
Management](../DATAHUB_INTEGRATION.md).

### URN conventions

- corpuser URN: `urn:li:corpuser:<email>`. The email-as-id form aligns with
  DataHub's `AUTH_OIDC_USER_ID_CLAIM=email` so a user logging into DataHub
  natively via Google OIDC resolves to the same URN DataSpoke wrote.
- corpGroup URN: `urn:li:corpGroup:<name>`, where `<name>` is the value of
  `/admin/conf.auth_datahub_corp_group` (default `dataspoke-users`) — the
  single marker group.

### Aspects DataSpoke writes

| Entity | Aspect | When |
|--------|--------|------|
| corpuser | `corpUserInfo` | On create; on name change. |
| corpGroup | `corpGroupInfo` | On marker-group lazy-create. |
| (none) | `corpUserCredentials` | **Never written by DataSpoke.** |

Role propagation uses the GraphQL `batchAssignRole` mutation, called after
every DataSpoke-side `users.role` write (registration and admin role
changes). Role *read* is **not on the request hot path** — the per-request
privilege check reads from DataSpoke `users.role` directly. The DataHub-side
read is used only by the nightly `auth-role-sync-daily` DAG to detect drift,
and reads the `RoleMembership` aspect directly (atomic single-role per
DataHub `RoleService`) rather than the `IsMemberOfRole` GraphQL relationship
index, which lags MCL→ES indexing. Group membership writes use `addGroupMembers` /
`removeGroupMembers`. User deletion uses the SDK's `hard_delete_entity` on
the corpuser URN (no aspect write; the entity and all its incoming /
outgoing references are removed).

### Mirror create sequence

1. Insert the DataSpoke `users` row with `role = 'Reader'` (transactional).
2. Write the DataHub `corpUserInfo` aspect.
3. Ensure the marker corpGroup (`/admin/conf.auth_datahub_corp_group`) exists
   on DataHub — re-assert both `Status(removed=false)` and `corpGroupInfo`
   aspects (idempotent overwrite; see [§Marker corpGroup](#marker-corpgroup))
   — then add the corpuser to it via `addGroupMembers`.
4. Propagate the user's `users.role` to DataHub via `batchAssignRole`. For
   self-registration this is always `Reader`; for admin-initiated creation
   (future scope), it would be whichever role the admin supplied.

Steps 2–4 are idempotent — re-running with the same email skips already-written
state. If any of steps 2–4 fail, DataSpoke deletes the local `users` row
(compensating hard-delete) and returns `503 DATAHUB_SYNC_FAILED`. A subsequent
re-registration with the same email resumes the sequence: the local row is
created fresh, and the DataHub-side aspect writes complete the partial corpuser
created on the previous attempt (idempotent — same data overwrites itself).

### Mirror delete sequence

1. Hard-delete the DataSpoke `users` row.
2. Hard-delete the DataHub corpuser via `hard_delete_entity`.

Group memberships, role assignments, and ownership references to the corpuser
are removed automatically by DataHub when the entity is hard-deleted, so no
separate group-removal step is required. If step 2 fails after step 1 succeeded,
the DataSpoke row is gone but the DataHub corpuser lingers as an orphan —
operators clean it up via DataHub directly (the orphan is visible in DataHub
listings). The order is chosen so the user is immediately unable to log into
DataSpoke even if the DataHub side fails.

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
| Created via | Auto-created by DataSpoke. Both `Status(removed=false)` and `corpGroupInfo` are re-asserted on every user registration (idempotent overwrite — defensive against the DataHub indexing race in which a previous failed attempt left only one of the two aspects committed). The `displayName` is reset to the group name and `members`/`admins`/sub-`groups` are reset to empty arrays on every touch, so the marker group **must not be used as a privilege carrier**. Operators wanting a different display name update `auth_datahub_corp_group` via `/admin/conf` instead of editing it on DataHub. |
| Rename behaviour | Changing `auth_datahub_corp_group` does not migrate existing memberships. Subsequent registrations create and populate the new group; previously-registered users remain in the old group, which becomes orphaned from DataSpoke's perspective but stays valid on DataHub. Operators avoid this by renaming the corpGroup on DataHub first, then updating the conf field. |

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
| `/spoke/*`, `/hub/*` | ✓ `GET` / `HEAD` / `OPTIONS` only | ✓ all methods | ✓ all methods |
| `/admin/*` | ✗ | ✗ | ✓ all methods |

Failure responses:

| Condition | Response |
|---|---|
| Reader attempting a write method on `/spoke/*` or `/hub/*` | `403 READ_ONLY_ROLE` |
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
JWTs. The middleware attempts JWT decode first; on failure (e.g., the token
is not a valid JWT shape), it falls back to opaque-token lookup against
`api_tokens.token_hash`. Either form populates the same request-context
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

DataSpoke `users.role` is the SSOT. The DataHub-side role assignment (via
`batchAssignRole`) is a one-way mirror used by DataHub UI for its own
authorization decisions. The two can diverge if an operator changes role
directly through the DataHub UI rather than via `PATCH /admin/users/{id}/role`.

A nightly Airflow DAG `auth-role-sync-daily` reconciles drift:

1. For each row in `users` (managed identities), read the corresponding
   corpuser's `RoleMembership` aspect directly (atomic single-role per
   DataHub `RoleService`). The `IsMemberOfRole` GraphQL relationship index
   is not used — it lags MCL→ES indexing and transiently shows roles that
   were already overwritten in the aspect.
2. Compare to `users.role`.
3. On divergence: re-assert `users.role` to DataHub via `batchAssignRole`.
   **DataSpoke wins.**
4. Emit an `AUTH.ROLE_SYNC_FIXED` event per fix (event_type, user_id,
   datahub_role_observed, dataspoke_role_authoritative, occurred_at).

The auto-fix is intentional: DataSpoke is the SSOT, so any DataHub-side
drift is by definition a mistake to be corrected. The DAG iterates only
rows in DataSpoke's `users` table — DataHub-only corpusers (e.g., a
super-admin not managed by DataSpoke) are out of scope. Operators who
need a DataHub-only role assignment keep that corpuser out of the
DataSpoke `users` table entirely.

The DAG's per-user GraphQL fan-out is bounded (one round trip per managed
corpuser per day). For large deployments this could be batched via
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
| `PATCH /admin/users/{id}/role` | Update `users.role` (Admin / Editor / Reader) and propagate to DataHub via `batchAssignRole`. DataSpoke is SSOT; the DataHub-side mirror is one-way. |
| `DELETE /admin/users/{id}` | Hard delete (mirror sequence above). |
| `PATCH /admin/conf` | Includes `auth_datahub_corp_group` (string, default `dataspoke-users`) — names the marker corpGroup; auto-created on first user registration if missing. |

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
| Login identifier | `dataspoke@dataspoke.local` (DataHub corpuser URN `urn:li:corpuser:dataspoke@dataspoke.local`) |
| Display name | `DataSpoke Admin` |
| Initial password | `dataspoke` |
| Role | `Admin` |
| DataHub mirror | Full create sequence (`corpUserInfo` → marker group → `batchAssignRole`). On mirror failure the local row is compensating-deleted and the bootstrap call returns `503 DATAHUB_SYNC_FAILED`. |

The dev install runs `helm-charts/bin/post-install/seed-admin-user.sh`
after the API pod is Ready; prod operators run the same script (or call
`/internal/admin/bootstrap` directly) once after pre-creating
`dataspoke-secrets`. Both paths must rotate the default password via
`PATCH /auth/me` before going to production — the install script logs a
warning that explicitly says so. The bootstrap admin is otherwise a
normal Admin user: it can be hard-deleted via `DELETE /admin/users/{id}`
once another Admin exists, and the bootstrap endpoint will recreate it
on the next install only if zero Admin rows remain.

---

## Failure Modes

| Failure point | Behaviour | User-visible outcome | Operator action |
|---|---|---|---|
| DataHub unreachable during `POST /auth/register` | Compensating hard-delete of the DataSpoke `users` row after the first DataHub-side step fails. | `503 DATAHUB_SYNC_FAILED`. | Resolve DataHub connectivity; user retries registration. |
| DataHub corpuser already exists from a prior partial registration | Mirror sequence resumes from the failing step (idempotent aspect writes). | Registration succeeds. | None. |
| Marker corpGroup missing on DataHub | Step 3 of the mirror create sequence auto-creates it. | Registration succeeds. | None. |
| SMTP peripheral missing during password-reset request | Request refuses; no DB write. | `503 PERIPHERAL_NOT_CONFIGURED`. | Admin configures `/admin/peripherals/smtp`. |
| SMTP configured but delivery fails (transport error, auth rejection, queue full) during password-reset request | Request refuses; no DB write — the token row is written only after `send_email` returns successfully. | `503 STORAGE_UNAVAILABLE` with a static message; the underlying SMTP error is logged but not echoed to the client. | Inspect API logs for the upstream cause; fix the SMTP path and retry. |
| Redis unreachable during refresh or revoke | Refresh/revoke fail-closed. | `503 STORAGE_UNAVAILABLE`. | Restore Redis. |
| Google OAuth state mismatch on callback | Callback aborts before token issuance. | `400 OAUTH_STATE_MISMATCH`. | User retries the OAuth flow. |
| Google OAuth callback receives ID token with `email_verified=false` | Callback rejects the token; no user is created or logged in. | `400 OAUTH_EMAIL_NOT_VERIFIED`. | User verifies their Google account email and retries. |
| Role change via `/admin/users/{id}/role`: DataSpoke write succeeds, DataHub propagation fails | The new role takes effect immediately on the DataSpoke API; DataHub-side stays stale until the nightly reconciliation DAG re-asserts. | The admin call returns `200` (DataSpoke side succeeded); a warning log records the propagation failure. | None — DAG handles. Manual recovery: re-PATCH the role. |
| Nightly role reconciliation finds a divergence | DAG re-asserts `users.role` to DataHub. | Operator visible via the `AUTH.ROLE_SYNC_FIXED` event row. | None unless the divergence was intentional (DataHub-only super-admin); in that case, exclude the corpuser from the marker group. |
| API token revoked while in use | Next request with the token fails. | `401 TOKEN_REVOKED`. | None — expected behaviour. |
| API token whose owner was demoted | Effective privilege drops immediately to `min(snapshot, current role)`. | Write attempts return `403 READ_ONLY_ROLE`. | None — expected behaviour. |
| DataHub hard-delete fails after DataSpoke `users` row was deleted | Orphan corpuser remains in DataHub. The user cannot log into DataSpoke; the corpuser cannot be used to log into DataHub natively because DataSpoke never wrote `corpUserCredentials`. | `200 OK` to the admin caller (DataSpoke side succeeded). | Operator hard-deletes the orphan corpuser via DataHub directly. |

---

## Security Considerations

### Email verification omitted by design

Self-registration trusts the supplied email without a verification round-trip.
This admits a typosquatting class of abuse — a registrant could claim
`ceo@example.com` and receive a corresponding DataHub corpuser at that URN.
The mitigation set:

- `POST /auth/register` is rate-limited (per the API rate-limit middleware) to
  raise the cost of mass registration.
- Default role is Reader. A typosquatted corpuser cannot edit metadata or
  manage policies without an admin explicitly promoting it.
- Admin hard-delete reverses the situation cleanly: the impostor's corpuser
  is hard-deleted (along with all its references in the DataHub graph), and
  the email is immediately free for re-registration by the legitimate owner,
  which re-runs the mirror create sequence.

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
  changes, user-mirror writes) use the single pre-configured admin-level
  service token from `dataspoke-datahub-secret`. DataHub's aspect-level
  audit attribution therefore points to the service-token's corpuser, not
  the real DataSpoke user. User-level audit lives in the DataSpoke `events`
  table. See [DATAHUB_INTEGRATION §Service Credential Model](../DATAHUB_INTEGRATION.md#service-credential-model).
- **Per-feature fine-grained authorisation.** The baseline gates routes by
  prefix (`/spoke/*`, `/hub/*`, `/admin/*`) and HTTP method × role;
  per-resource ACLs on individual datasets, runs, or reviews are out of
  scope and live in DataHub policies.
