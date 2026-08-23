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
  `session_epoch`, `created_at`, `updated_at`. At least one of
  `password_hash` and `google_sub` must be set (`ck_users_auth_method`).
  `session_epoch` is the per-user JWT generation counter — see [§Session
  epoch](#session-epoch). User deletion is hard delete — no `deleted_at`
  column.
- **`api_tokens`** — long-lived personal access tokens minted by users for
  non-interactive clients. Columns: `id (uuid pk)`, `user_id (fk)`, `name`,
  `token_hash (sha256, unique)`, `role_snapshot`, `created_at`,
  `last_used_at`, `expires_at (nullable)`, `revoked_at (nullable)`.
- **`password_reset_tokens`** — single-use reset tokens. Columns:
  `token_hash (sha256, pk)`, `user_id (fk)`, `expires_at`, `used_at`.

Full column types, indexes, and constraints in
[BACKEND_SCHEMA](BACKEND_SCHEMA.md). Role is a first-class column on `users` —
see [Privilege Model](#privilege-model) for how it gates DataSpoke routes.

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
| No | Yes, and that row has `google_sub IS NULL` | Bind `google_sub` onto the row, refresh display name from the Google profile, run the [credential reset](#credential-reset-on-link), and log in. |
| No | Yes, and that row already carries **this** `sub` | Log in, exactly as the `sub`-known branch. No bind, no reset, no epoch bump, no event. |
| No | Yes, and that row carries a **different** `google_sub` | Refuse — `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT`. No row is modified and no session is issued. |
| No | No | Create a fresh `users` row with `password_hash=null` and `role = 'Reader'`. Log in. |

The bind branch refreshes `name` from the Google claim for the same reason the
`sub`-known branch does: the row now belongs to the verified identity, and the
display name it presents in `/auth/me`, `/admin/users`, and the app shell must
be that identity's rather than the previous holder's.

The row-already-carries-this-`sub` outcome is the resolution of a race, not a
distinct user intent: two concurrent or retried callbacks can both miss the
`sub` lookup, and the one that reaches the row lock second finds the first
callback's bind already committed. It is the same request the `sub`-known branch
serves, so it gets the same answer — a plain login. Re-running the reset there
would kill the session the first callback just issued.

The bind branch therefore applies only to an unbound row. One DataSpoke row
carries at most one Google identity at a time, and one Google identity belongs
to at most one row: a bind whose incoming `sub` is already held by a
different row loses the `UNIQUE(google_sub)` race and fails
`GOOGLE_ACCOUNT_LINKED_ELSEWHERE`. An operator can release a stale binding
with [`DELETE /admin/users/{id}/google`](#admin-surface).

#### Credential reset on link

The Google identity is provider-verified; the row it binds onto may not be — the
email on it was never verified (see [§Email verification omitted by
design](#email-verification-omitted-by-design)). When the two meet, the verified
identity wins the row, and every credential that existed on the row before the
bind is invalidated in the **same transaction** as the bind:

| Credential | Invalidation |
|---|---|
| Password | `password_hash` set to `NULL`. The row stays valid under `ck_users_auth_method` because `google_sub` is now set. |
| API tokens | Every active token for the user is revoked (`revoked_at = now()`). |
| JWT sessions | All outstanding access and refresh tokens are killed by incrementing `session_epoch` — see [§Session epoch](#session-epoch). |
| Password-reset tokens | Unused `password_reset_tokens` rows for the user are deleted. |

The reset covers the whole pre-bind credential surface, so a party who held the
row beforehand keeps no way back into it — not the password, not a session, not
a minted API token, not a pending reset link. Rationale and threat model in
[§Account pre-hijacking on Google link](#account-pre-hijacking-on-google-link).

Exactly one `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event (`entity_type = user`,
`entity_id` = the user id) per bind that actually writes `google_sub`, recording
what was cleared. Such a bind always clears at least a password: it reaches an
existing row only by email match, that row is unbound (`google_sub IS NULL`),
and `ck_users_auth_method` then forces `password_hash IS NOT NULL`. A reset that
invalidates nothing is not a reachable state. A callback that finds the row
already carrying its own `sub` writes nothing and emits nothing.

Accepted trade-off, stated plainly: a legitimate password user who signs into
DataSpoke with Google for the first time **loses password login and is signed out
of every other session**. They re-establish a password via `PATCH /auth/me`,
authenticated by the session the Google callback just issued. This is the
deliberate price of making the link path safe against a pre-registered squatter —
the bind path cannot distinguish the account's owner from someone who claimed the
address first, so it treats both the same way.

#### Callback failure surface

Both `/auth/google/*` routes are reached only as full-page browser navigations —
Google redirects the user agent to the callback, and the login route is a link the
user clicks. Neither is ever called by an API client. **Every outcome their
handlers produce is therefore a 302, never the JSON error envelope**: success
redirects to the configured post-login target with the refresh cookie set, and
every raised error redirects to the public UI page `/oauth-error` with no cookie
set. The contract table (including which failures carry an `error` query
parameter and what a failure leaves behind), the middleware-plane exception, and
the redirect-target derivation are in [API §OAuth browser-redirect
contract](../API.md#oauth-browser-redirect-contract).

Each code that reaches the page gets its own copy; an unrecognised or absent code
falls back to generic wording. `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` is the one
the page exists for: it is the steady state of a re-issued address rather than a
transient fault, the holder cannot self-serve out of it, and recovery is the
three-step admin sequence in [§Admin unbind](#admin-unbind) — which the page
states rather than leaving the user to discover. Per-code copy in
[FRONTEND_BASIC §OAuth error page](FRONTEND_BASIC.md#oauth-error-page-oauth-error).

### Login

`POST /auth/token` with `{email, password}` issues an access JWT (15 min
lifetime, in response body) and a refresh JWT (7 d lifetime, as `HttpOnly`
cookie). The access JWT's claims are `sub`, `email`, `exp`, `iat`, and `ses` —
the session epoch the token was issued under ([§Session
epoch](#session-epoch)). The JWT does not encode role (see [Privilege
Model](#privilege-model)); role is read per-request from `users.role`, on the
same read that resolves `ses`.

### Refresh & revoke

`POST /auth/token/refresh` validates the refresh-cookie JWT, checks its `ses`
claim against the owner's current [session epoch](#session-epoch), checks the
Redis revocation list, and issues a fresh access token with the same identity
claims.
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

### Session epoch

Refresh tokens are revocable only per token hash in Redis, so there is no way to
evict a user's outstanding sessions from the revocation store alone — the server
does not enumerate them. `users.session_epoch INTEGER NOT NULL DEFAULT 0`
supplies the per-user generation counter that closes that gap.

- **Rule.** Both the access JWT and the refresh JWT carry a `ses` claim holding
  the epoch they were issued under. A JWT whose `ses` claim is absent, or does
  not equal the owner's current `session_epoch`, is rejected
  `401 UNAUTHORIZED`.
- **Enforcement points.** The bearer-JWT authentication path and
  `POST /auth/token/refresh`. Both already read the owner's `users` row for the
  role check, so the epoch comparison rides on that existing read — no extra
  round trip. The API-token path needs no check: those rows are revoked
  outright.
- **Exactness.** A credential reset increments `session_epoch` by one in the
  same transaction as the other invalidations. Every token issued under the
  previous epoch is dead the instant that transaction commits, and the session
  token the OAuth callback mints afterwards reads the new epoch and is valid.
  No clock participates, so there is no granularity, no skew assumption between
  the DB clock and the token-issuing clock, and no window in which a token
  outlives the reset that killed it.
- **Who bumps it.** The two writes that change the row's Google binding: the
  [credential reset on link](#credential-reset-on-link) when a binding is
  established, and the [admin unbind](#admin-unbind) when one is released.
  A password change via `PATCH /auth/me` and
  `POST /auth/password/reset/confirm` deliberately leave it alone: both are
  performed by a caller who already controls the account, so a global sign-out
  would cost that user their other sessions to defend against nothing. A
  binding change is different in kind — it is where the row's authoritative
  identity moves, so the sessions issued under the old one do not carry over.

#### Serialization of credential-creating writes

The epoch evicts credentials that exist when the reset commits. A write already
in flight would otherwise slip past it: an authorisation checked before the
reset commits can produce a credential that lands after it, on a row that has
already changed hands.

**Credential-creating self-service writes therefore re-validate their
authorisation inside their own write transaction, under the `users` row lock.**
Four writes create a credential: the `password` field of `PATCH /auth/me`,
`POST /auth/api-tokens`, `POST /auth/password/reset/confirm`, and
`POST /auth/password/reset/request`. Each takes the `users` row lock and
re-checks, under it, the state that authorised it:

| Write | Re-check under the lock |
|---|---|
| `PATCH /auth/me` (`password`) | Re-compare the request's `ses` claim against the freshly read `session_epoch`; mismatch → `401 UNAUTHORIZED`. |
| `POST /auth/api-tokens` | Same `ses` re-comparison. Needed here in particular because the API-token authentication path runs no epoch check, so a token committed after the reset would otherwise stay live. |
| `POST /auth/password/reset/confirm` | Re-read the `password_reset_tokens` row, which the bind's delete has already removed; missing or used → the route's existing invalid-token failure. |
| `POST /auth/password/reset/request` | Re-compare `session_epoch` against the value read before the token row was prepared; if it has moved, complete **without** writing the token row. |

The request route's shape differs from the other three because a reset token it
mints is a live 15-minute password-write capability that no epoch governs — it
is not a JWT. Merely locking around the INSERT would not contain it: the bind
holds the lock, releases it on commit, and the insert then lands *after* the
bind's delete of unused rows, which sweeps only what is visible at that
statement. The epoch comparison is what closes it — a request whose read
predates the bind observes the increment and declines to write. It still
returns `204`, unchanged, since the route reports the same outcome for known
and unknown emails and must not become an oracle for account state.

The re-check is against whatever credential authorised the request, so the two
JWT rows above describe the JWT carrier only. `PATCH /auth/me` and
`POST /auth/api-tokens` are equally reachable with an API token (see [§API
Tokens](#api-tokens) — a PAT carries the same self-scoped `/auth/*`
privileges), and such a request has no `ses` claim to compare; it instead
re-reads its own `api_tokens` row under the same `users` row lock and fails
`401 TOKEN_REVOKED` once the reset has revoked it.

Because each takes the lock the bind transaction holds, none can commit before
it; because each re-reads after acquiring it, none can commit a credential
authorised by state the bind superseded. This is what makes the [credential
reset on link](#credential-reset-on-link) claim — that a party who held the row
beforehand keeps no way back into it — true rather than aspirational.

Only `users` takes an explicit row lock (`SELECT … FOR UPDATE`); `api_tokens`
and `password_reset_tokens` are never locked explicitly — each is locked only
implicitly, at the moment its own `UPDATE`/`DELETE`/`INSERT` statement runs.
When a single write path takes more than one of these locks, the explicit
`users` row lock is acquired first, and the implicit write locks that the
`api_tokens`/`password_reset_tokens` statements take follow in the order
`api_tokens` → `password_reset_tokens`; every multi-lock path in this section
follows it, and it should be preserved by any future one to avoid a lock-order
deadlock across concurrent requests. This does not mean `api_tokens` or
`password_reset_tokens` should ever gain an explicit `FOR UPDATE` lock — the
ordering constraint governs implicit write-lock acquisition, not a requirement
to add explicit locking to the other two tables.

The re-read-after-lock correctness described above also depends on Postgres
running at its default READ COMMITTED isolation level: DataSpoke must not
override the isolation level for these paths, since a re-read after acquiring
the `users` row lock observes the writer's just-committed change only because
READ COMMITTED lets each statement see newly-committed data. If the isolation
level is ever raised to REPEATABLE READ, these re-reads would surface as
serialization failures instead of the intended re-check behavior.

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
including `users.role` and the booleans `has_password` and `has_google` — the
presence of each authentication method, never the hash or the `sub`.
`has_password` is what tells a user whose password was cleared by a [credential
reset](#credential-reset-on-link) that setting one is the way back to password
login. `PATCH /auth/me` accepts `{name?, password?}`: both
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
establish the binding by signing into DataSpoke with Google once — binding
`google_sub` onto the existing row (see [§Google OAuth registration &
login](#google-oauth-registration--login)), which also clears that row's
password and tokens per the [credential reset](#credential-reset-on-link) —
after which the next reconciliation pass projects both facets. The bootstrap admin
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
Admins read other users' tokens through two surfaces — a deployment-wide
inventory and a per-user list — and revoke any of them, for incident response.
Minting is owner-only in every case: no route mints a token for anyone but its
caller.

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

On each request authenticated by an API token, the auth dependency computes:

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
The update is throttled to per-minute granularity — the authentication path
issues the `UPDATE` with a `WHERE` clause that makes it a no-op below 60s — so
a high-frequency client doesn't flood the DB. Stale updates from concurrent
requests are acceptable — `last_used_at` is for human inspection, not
race-sensitive billing.

The stamp is a side effect of authentication, not a step of it. It runs
after the token has already passed every validation check, and any failure
writing it — a lost connection, a pool timeout, a session that cannot be
opened — is logged at `ERROR` and swallowed rather than surfaced: the column
keeps its prior value and the request continues with the identity it earned. See
[BACKEND §Best-Effort Operations](BACKEND.md#best-effort-operations) for the
logging convention this follows and why this stamp is the one operation in that
list that logs at `ERROR` rather than WARNING. A consequence for anyone
reading the column: a stale or NULL `last_used_at` is not evidence the token went
unused, because a swallowed stamp leaves exactly the value a genuinely unused
token carries — the `ERROR` log record is the only trace of that case. The
swallow covers the stamp only: the three `401` outcomes above
(`INVALID_API_TOKEN`, `TOKEN_REVOKED`, `TOKEN_EXPIRED`) are decided before it
and still raise.

### Lifecycle endpoints

The API contract lives in [API §Auth](../API.md#auth) and
[API §Admin](../API.md#admin-admin). Summary:

| Endpoint | Purpose |
|----------|---------|
| `GET /auth/api-tokens` | List own tokens (content key `tokens: [{id, name, role_snapshot, created_at, last_used_at, expires_at}]` under the standard pagination envelope — never the raw token) |
| `POST /auth/api-tokens` | Mint a new token (body `{name, expires_at?}`); response includes the raw token in `{token: "dsk_..."}` — only time it is returned plain |
| `DELETE /auth/api-tokens/{id}` | Revoke an own token (sets `revoked_at = now()`) |
| `GET /admin/users/{id}/api-tokens` | List one user's tokens (admin), in the admin item shape |
| `GET /admin/api-tokens` | List tokens across all users (admin) — the deployment-wide inventory; optional `user_id` owner filter, sortable by `created_at`/`last_used_at` |
| `DELETE /admin/users/{id}/api-tokens/{token_id}` | Revoke a user's token (admin; incident response) |

The two admin reads share an item shape distinct from the self read's; both are
enumerated in [API §Admin](../API.md#admin-admin). Revocation needs no route of
its own on the inventory — each item carries the `user_id` that addresses
`DELETE /admin/users/{id}/api-tokens/{token_id}`.

### Revoked-token visibility

Both admin reads exclude revoked rows by default and take `include_revoked=true`
to bring them back, following the project's convention of keeping withdrawn
records out of the default view behind an explicit opt-in. A revoked row grants
nothing — authentication fails `401 TOKEN_REVOKED` — so carrying it in the
default list pads the answer with credentials already dealt with. Those rows stay
reachable because incident review needs to see when a credential was withdrawn,
which is what `revoked_at` carries.

`revoked_at IS NULL` is the whole of the default filter. Expiry is not filtered:
a token past its `expires_at` authenticates nothing (`401 TOKEN_EXPIRED`) yet sits
in the default page like any other row. What separates the two is the item's own
`expires_at`, which is why it is on the shape — a reader counting what is usable
reads that field per row rather than treating the page as the usable set.

Both routes express their filter, ordering, and page bounds in SQL, so a request
transfers and materialises one page rather than the whole matching set. The
ordering still ranges over that set: `api_tokens` is indexed for the per-user
active lookup, not for the sort keys these routes offer.

Either ordering places nulls last and is tiebroken by token id, so paging an
inventory returns each token exactly once regardless of the requested `sort`.
Both properties are load-bearing rather than cosmetic: ties are certain under
`last_used_at`, where every never-used token shares a null, and reachable under
`created_at`, where tokens minted in one transaction share a timestamp — an
unspecified order within a tie can shift between the page-1 and page-2 queries and
drop a live credential from every page. Nulls-last keeps `last_used_at_desc`, the
ordering that asks which credentials are in use, from opening with the ones that
never have been.

`GET /auth/api-tokens` excludes revoked rows and offers no opt-in. A revoked
token is nothing its owner can act on — it cannot be used, un-revoked, or revoked
again — and `revoked_at` is not on the self item shape, so there is no withdrawal
timeline to read there either. Audit of withdrawn credentials is an admin concern,
served by the two routes above.

### Admin revoke audit

`DELETE /admin/users/{id}/api-tokens/{token_id}` acts on a credential the caller
does not own, and by design applies no ownership check — that is what makes it
usable for incident response, and what makes it worth recording. It emits one
`AUTH.API_TOKEN_REVOKED` event against the token's owner; the catalogue entry,
including the detail keys, is in
[BACKEND §Event Catalogue](BACKEND.md#event-catalogue). Setting `revoked_at` is
the whole of what ends a token's life, so the write **is** the security event:
absent the record, the only trace of an admin killing someone else's credential is
the column's new value, which names neither who set it nor when anyone noticed.
The event carries the token and its owner, not the acting admin — the request log
is where the principal lives.

Every other path by which a PAT dies already leaves a record: the [credential
reset on link](#credential-reset-on-link) counts the tokens it revokes in its own
event, and the [admin unbind](#admin-unbind) emits one for the authentication
method it removes. The by-hand admin revoke completes that picture, which matters
more here than it would for an incident-response corner: the deployment-wide
inventory makes cross-user revocation an ordinary workflow rather than a rare
intervention.

`DELETE /auth/api-tokens/{id}` emits nothing. A user retiring their own token is
routine hygiene with no privilege asymmetry to audit, and the outcome is already
visible to the only party it concerns. Recording it would bury the admin event —
the one that says someone acted on a credential that was not theirs — under the
ordinary traffic of self-service.

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
| `DELETE /admin/users/{id}/google` | Release the row's Google binding — see [§Admin unbind](#admin-unbind). |
| `PATCH /admin/conf` | Includes `auth_datahub_corp_group` (string, default `dataspoke-users`) — names the marker corpGroup; asserted once per reconciliation pass. |

`/admin/*` routes require `users.role = 'Admin'` — checked per-request per
[Privilege Model](#privilege-model). The JWT carries no admin claim.

### Admin unbind

A binding is permanent from the user's side: no self-service route releases it,
and the callback refuses to rebind a bound row. That is the correct default —
silent rebinding is the pre-hijacking hole — but it strands a row whose Google
`sub` has ceased to exist. The common case is a re-issued Workspace address: the
directory account is deleted and recreated, the new one carries a new `sub`, and
the row still names the old one, so the address's rightful holder is refused
`EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` on every attempt — landing, each time, on
the `/oauth-error` page that carries the three-step sequence below
([§Callback failure surface](#callback-failure-surface)).

`DELETE /admin/users/{id}/google` is the non-destructive remedy. It clears
`google_sub` and increments `session_epoch` — unbinding is a credential change,
so sessions established under the released binding do not survive it — and emits
one `AUTH.GOOGLE_UNBOUND` event (`entity_type = user`, `entity_id` = the user
id) so the removal of an authentication method leaves a record of who was
unbound and when. The row then resolves as unbound, and the next Google sign-in
at that address binds the new `sub` through the ordinary bind branch, credential
reset included.

It does **not** revoke the row's API tokens, and the PAT authentication path
runs no epoch check, so tokens minted before the unbind keep working. The
asymmetry is deliberate: an unbind returns the row to its existing holder rather
than handing it to someone new, so the tokens still belong to whoever minted
them — and if the row does later change hands, the bind's [credential
reset](#credential-reset-on-link) revokes them then. An admin who wants them
gone regardless revokes them individually via
`DELETE /admin/users/{id}/api-tokens/{token_id}`.

The route is idempotent: an already-unbound row is left untouched and still
answers `204`. There is no binding to release, so there is no credential change,
and bumping the epoch there would sign the user out of every session for nothing
— against the rule that only a row changing hands invalidates sessions
([§Session epoch](#session-epoch)).

The route refuses with `409 GOOGLE_IS_ONLY_AUTH_METHOD` when the row has no
`password_hash`: clearing `google_sub` would violate `ck_users_auth_method` and
leave a row nobody can authenticate as.

**That refusal is the normal state of a bound row, not an edge case.** A row is
password-less at the moment it becomes bound — the bind nulls `password_hash`,
and a Google-native signup creates the row without one — and it regains a
password only if someone later sets one through `PATCH /auth/me` or
`POST /auth/password/reset/confirm`. Releasing a stale binding therefore takes a
sequence rather than a single call:

1. The address's new holder runs `POST /auth/password/reset/request` and
   completes `POST /auth/password/reset/confirm`. The row now has a
   `password_hash`.
2. The admin calls `DELETE /admin/users/{id}/google`, which now succeeds.
3. The new holder signs in with Google. The ordinary bind branch binds the new
   `sub` and its credential reset nulls the password again, returning the row to
   the standard bound shape.

The step-1 prerequisite is deliberate rather than an obstacle to route around.
The reset round-trip goes to the mailbox at that address, so completing it
proves the new holder controls it — the same ownership proof the bind path
relies on, obtained the only other way DataSpoke has. An admin who unbinds
without it would be handing the row to whoever asked.

Note what the sequence preserves: the row keeps its `role` across the handover.
Reclaiming a row is not the same as issuing a fresh one, so an admin who does
not intend the new holder to inherit the previous holder's privileges demotes
the row via `PATCH /admin/users/{id}/role` first. An admin who wants none of
this — no reclamation, no inherited state — removes the user with
`DELETE /admin/users/{id}` instead, accepting that it also hard-deletes the
DataHub corpuser at that URN.

DataHub consequence: an unbound row is not projected
([§Identity-binding requirement](#identity-binding-requirement)), so the nightly
pass moves it to `skipped_unbound`. The role already assigned on the corpuser
stays — retraction is deletion-only
([§Projection retraction sequence](#projection-retraction-sequence)) — and is
re-asserted once the row is bound again.

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
| A `users` row has no `google_sub` (password-only account, or the bootstrap admin) | Neither projection path writes anything for it; the reconciliation pass counts it `skipped_unbound`. | None on the DataSpoke side; the user's DataHub role is whatever DataHub itself assigns. | None. The user binds the identity by signing into DataSpoke with Google once — at the cost of that row's password and API tokens ([credential reset](#credential-reset-on-link)) — after which the next pass projects both facets. |
| DataHub peripheral unconfigured when the nightly pass runs | The pass returns a no-op result rather than failing — operating before DataHub is wired is a supported steady state. | None. | None; the pass reconciles once the peripheral is configured. |
| Marker corpGroup missing on DataHub | The reconciliation pass creates it before projecting any membership. | None. | None. |
| Marker corpGroup assert fails at the start of a reconciliation pass | The pass aborts before its per-user loop rather than degrading. `addGroupMembers` rejects an unresolvable group URN, so a pass that continued would fail every group facet while reporting a clean run over the role facet. | Retryable error response; no counter result is returned. | None — Airflow retries the run. |
| `DELETE /admin/users/{id}/google` on a row with no `password_hash` | Refused before any write — clearing the only authentication method would leave a row nobody can authenticate as. | `409 GOOGLE_IS_ONLY_AUTH_METHOD`. | Remove the user with `DELETE /admin/users/{id}` if that is the intent. |
| A Google bind commits while `POST /auth/password/reset/request` is in flight | The request declines its token INSERT on the epoch re-check ([§Serialization of credential-creating writes](#serialization-of-credential-creating-writes)), but the email has already been sent — the route sends before it writes. The recipient holds a link that matches no row. | `204` to the caller (unchanged — the route reports the same outcome for every address, so it cannot signal this either); the emailed link fails `400 INVALID_RESET_TOKEN` when used. | None. The user signs in with Google, which now owns the row, and requests a fresh reset if they still want a password. |
| `POST /auth/password/reset/confirm` with a token whose `users` row has been hard-deleted | Resolves as the route's ordinary invalid-token outcome; no existence signal is emitted. | `400 INVALID_RESET_TOKEN` (not a `404`). | None — the intended contract; a reset link cannot report whether an account exists. |
| SMTP peripheral missing during password-reset request | Request refuses; no DB write. | `503 PERIPHERAL_NOT_CONFIGURED`. | Admin configures `/admin/peripherals/smtp`. |
| SMTP configured but delivery fails (transport error, auth rejection, queue full) during password-reset request | Request refuses; no DB write — the token row is written only after `send_email` returns successfully. | `503 STORAGE_UNAVAILABLE` with a static message; the underlying SMTP error is logged but not echoed to the client. | Inspect API logs for the upstream cause; fix the SMTP path and retry. |
| Redis unreachable during refresh or revoke | Refresh/revoke fail-closed. | `503 STORAGE_UNAVAILABLE`. | Restore Redis. |
| Redis unreachable during rate limiting | The auth-route limiter fails closed, matching the refresh path above; after a storage failure it fast-denies for a short cooldown rather than re-paying the connect timeout on each request. Every other route keeps its single per-caller budget on the default limiter, which falls back to per-process in-memory counting. Neither plane stalls the event loop while Redis hangs — both check limits on a worker thread under bounded socket timeouts ([§Client-IP attribution for rate limiting](#client-ip-attribution-for-rate-limiting)). | Auth routes (`/auth/register`, `/auth/token`, the password-reset pair, and `/auth/google/{login,callback}`): `503 STORAGE_UNAVAILABLE` — on the two Google routes this is the limiter's envelope, ahead of the handler's redirect contract. Other routes: served, but the effective budget multiplies by the number of API worker processes, so the global limit is not enforced for the duration. | Restore Redis, then restart the API pods. The fallback is a sticky per-process flag whose recovery probe backs off exponentially, so a worker that flipped to memory does not necessarily resume shared counting when Redis returns. |
| Google OAuth credentials or the OAuth-state secret unset | Both `/auth/google/*` routes refuse before contacting Google. | 302 to `/oauth-error?error=OAUTH_NOT_CONFIGURED`. | Operator sets `DATASPOKE_GOOGLE_OAUTH_CLIENT_{ID,SECRET}` and `DATASPOKE_OAUTH_STATE_SECRET`. |
| Google OAuth state mismatch on callback | Callback aborts before token issuance. | 302 to `/oauth-error?error=OAUTH_STATE_MISMATCH` ([§Callback failure surface](#callback-failure-surface)). | User retries the OAuth flow. |
| Google OAuth callback receives ID token with `email_verified=false` | Callback rejects the token; no user is created or logged in. | 302 to `/oauth-error?error=OAUTH_EMAIL_NOT_VERIFIED`. | User verifies their Google account email and retries. |
| Google identity binds onto an unbound row matched by email | The bind and the [credential reset](#credential-reset-on-link) commit in one transaction; one `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event records what was cleared. | The user is logged in via Google. Password login and every previously minted API token stop working; `GET /auth/me` reports `has_password: false`. | None. The user sets a new password via `PATCH /auth/me` and re-mints any API tokens they still need. |
| A JWT presented after its owner's `session_epoch` was incremented | The bearer path and `/auth/token/refresh` both reject on the `ses` comparison. | `401 UNAUTHORIZED`; the frontend clears the session and redirects to `/login`. | None — expected behaviour after a credential reset. |
| Google callback whose email matches a row already bound to a **different** Google `sub` | Callback refuses before any write; no row is modified and no session is issued. | 302 to `/oauth-error?error=EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT`, whose page states the unbind sequence. | Admin releases the stale binding with `DELETE /admin/users/{id}/google` ([§Admin unbind](#admin-unbind)); the next sign-in binds the current `sub`. |
| Two callbacks race to bind the same Google `sub` onto different rows, or a bind commits between one callback's resolution and its write | The losing bind violates `UNIQUE(google_sub)`; its transaction rolls back whole, so that row keeps its password, tokens, and session epoch. | 302 to `/oauth-error?error=GOOGLE_ACCOUNT_LINKED_ELSEWHERE`. | None — the winning bind stands and the user retries, which now resolves by `sub`. |
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
- `POST /auth/register` is rate-limited by the fail-closed auth limiter that
  governs the credential-accepting routes, not by the default middleware
  limiter. This bounds *bulk automated* registration only. It is not a meaningful
  barrier to the targeted squatting described above, which needs exactly one
  request per address claimed. The limit is per client only in deployments that have named
  their trusted proxies and preserve the real client IP; by default all
  unauthenticated traffic shares one bucket (see
  [Client-IP attribution for rate limiting](#client-ip-attribution-for-rate-limiting)).
- Default role is Reader. A typosquatted account cannot edit metadata or
  manage policies without an admin explicitly promoting it.
- **The squatted row does not survive the owner's arrival.** When the genuine
  owner of the address signs into DataSpoke with Google, their verified identity
  binds onto that very row and takes it — see [§Account pre-hijacking on Google
  link](#account-pre-hijacking-on-google-link). The squatter is left holding no
  credential on it.
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

### Account pre-hijacking on Google link

Open self-registration plus a link-on-email-match OAuth path is the classic
pre-hijacking shape: an attacker registers `cto@company.com` before its owner
does, waits for the owner to sign in with Google, and rides the row the owner
now believes is theirs. The two ingredients of the attack are an unverified
claim to an address and a link step that leaves the claimant's credentials in
place. DataSpoke keeps the first — registration is unverified by design — and
removes the second.

The [credential reset on link](#credential-reset-on-link) is the containment.
Its coverage has to be total because each credential it clears is an
independent re-entry path: a squatter left holding only a long-lived API token
retains the owner's account just as effectively as one left holding the
password.

This complements, rather than duplicates, the containment described under
[§Email verification omitted by design](#email-verification-omitted-by-design).
That one is about the DataHub side, and rests on the [identity-binding
requirement](#identity-binding-requirement): an unbound row never projects onto
`urn:li:corpuser:<email>`, so a squatter's role cannot land on the real person's
DataHub identity. It says nothing about what happens *when a binding appears* —
and a binding is exactly what the owner's first Google sign-in creates. The
credential reset governs that moment: the row becomes projectable and, in the
same transaction, becomes exclusively the verified identity's.

Residual exposure that the reset does not remove:

- Anything the squatter did while holding the row before the owner arrived —
  metadata they read, configs they wrote at whatever role they held — stands.
  The reset invalidates credentials, not history. Default `Reader` bounds this
  to reads unless an admin promoted the row.
- An address the genuine owner never signs into DataSpoke with stays squatted
  indefinitely; nothing triggers a reset that has no bind behind it.

### Client-IP attribution for rate limiting

Each of the two rate-limit planes buckets a request by a key it derives from
that request, and the two derive it differently on purpose
([API.md §Middleware Stack](../API.md#middleware-stack) owns the budgets, the
route exemptions, and the 429 contract).

**The default plane keys on caller identity**, taking the first of: the JWT
`sub` claim; a fingerprint (truncated SHA-256) of an opaque `dsk_…` API token;
the `sub` of a signature-verified refresh cookie — consulted only when the
request presents no `Authorization: Bearer` header at all, so a request carrying
an unreadable bearer token keys on the address even if a valid cookie rides
along; the observed client address.
This is a *fairness* key, not a security boundary — the caller chooses which of
those credentials it presents. Each branch keeps a class of caller out of the
address bucket: API-token clients (the end-user plugin carries no other
credential) would otherwise share one budget deployment-wide, and
`POST /auth/token/refresh` — a public route that authenticates by cookie alone —
would let one client exhaust every user's ability to refresh.

**The auth plane keys on the observed client address, unconditionally.** The
routes it governs accept or issue credentials, so their callers are
unauthenticated by definition and every identity in such a request is one the
caller chose; both
`POST /auth/register` and `POST /auth/token` moreover *hand out* exactly such a
credential, so any request-derived key would let the limited party mint itself a
fresh budget per credential acquired. This limiter is the only brute-force
control on `POST /auth/token` — DataSpoke has no account lockout — and a bound
the limited party can reset is not a bound on guessing rate. The price of the
choice is that the address has to be attributed correctly, which is what the
rest of this section is about: if it collapses to a single value, all
unauthenticated traffic shares one bucket.

Being the only such control also shapes how that plane fails and where its
counters live:

- **Fail-closed.** No in-memory fallback and no swallowed storage errors: an
  unreachable Redis makes these routes answer `503 STORAGE_UNAVAILABLE`, the
  same posture `/auth/token/refresh` takes on revocation lookups. The control
  cannot weaken silently — it becomes a visible outage. The default plane keeps
  its in-memory fallback instead, because denying every read route on a Redis
  outage is a worse outcome than an imprecise shared budget.
- **Bounded storage timeouts.** The limiters' Redis client pins explicit connect
  and read timeouts rather than inheriting the OS default, which runs to minutes
  against a blackholed SYN. A fail-closed limiter never marks its storage dead,
  so every auth request would otherwise re-pay that wait; a short post-failure
  cooldown fast-denies without touching the socket until Redis is worth
  retrying.
- **Off the event loop.** The counter store is a synchronous Redis client, so
  both planes run the limit check on a worker thread. Checked inline it would
  park the whole uvicorn worker for the socket timeout on every request while
  Redis is unreachable, freezing every other in-flight request with it —
  including the Kubernetes probes on `/health`, which turns a Redis stall into a
  pod restart loop.
- **A dedicated Redis logical DB.** The counters live apart from the application
  cache, the `SET NX` concurrency locks, and the refresh-revocation set, so
  nothing in the key-eviction path of ordinary cached data can clear a
  brute-force counter.

The observed address is the real client only if **every hop between client and
API preserves it**. Two classes of hop matter:

- **The API's own trust boundary, which is closed by default.** The API's
  uvicorn server honours the forwarded headers only when the immediate peer is
  in the trusted list supplied by the `config.trustedProxyIps` chart value, and
  it then walks the `X-Forwarded-For` chain from right to left, taking the
  first address *not* in that list as the client. Every entry is therefore a
  party permitted to *name* the client address, not merely to relay it: a
  caller whose own source address falls inside the list can forge the header,
  rotate it per request, and mint a fresh bucket each time. The value names the
  deployment's ingress controller and nothing wider — a private-range envelope
  would admit every in-cluster pod and every VPN or peered-network caller able
  to reach the API pod. It defaults to loopback only, trusting no proxy at all,
  so per-client bucketing is opt-in and an unconfigured deployment buckets all
  unauthenticated traffic together.
  [HELM_CHART.md](HELM_CHART.md#tier-1--app-runtime-dataspoke_) owns the value
  and its reasoning.
- **Every hop in front of it**, whose requirement is topology-dependent. Where
  an L4 LoadBalancer fronts the ingress controller (dev managed mode), the
  controller Service needs `externalTrafficPolicy: Local` — under the default
  `Cluster` policy kube-proxy masquerades the source address to a node IP, so
  the API sees at most one address per node no matter which proxies it trusts.
  Where an L7 load balancer (ALB or similar) fronts the controller, the client
  address arrives in `X-Forwarded-For` and `externalTrafficPolicy` is
  immaterial; the controller must instead be configured to trust and extend
  that header rather than replace it with the peer it sees — ingress-nginx
  discards it by default (`use-forwarded-headers: false`).

DataSpoke asserts neither of these on the operator's ingress path; they are
operator prerequisites. The dev managed-mode nginx-ingress Service leaves
`externalTrafficPolicy` at its `Cluster` default, so dev rate-limit buckets are
per node rather than per client.

**The same trust gate governs `X-Forwarded-Proto`, which makes the value
OAuth-affecting.** When the peer is trusted, the API takes its request scheme
from that header. `GET /auth/google/login` derives the OAuth `redirect_uri`
from the request URL, so the scheme the API believes it was reached over is the
scheme it sends to Google. Trusting a TLS-terminating proxy therefore flips the
generated `redirect_uri` from `http://` to `https://` — correct, but Google
rejects the flow with `redirect_uri_mismatch` until the matching `https://`
entry is registered as an authorised redirect URI on the OAuth client. Widening
`config.trustedProxyIps` is not a rate-limiting-only change.

### OAuth flow hardening

State and nonce are stored in the signed Starlette session cookie (HMAC
key `DATASPOKE_OAUTH_STATE_SECRET`); authlib generates fresh random
values on every `/auth/google/login` and validates them on callback.
Mismatches fail `OAUTH_STATE_MISMATCH` without attempting token
exchange. The callback rejects ID tokens with `email_verified=false`
(`OAUTH_EMAIL_NOT_VERIFIED`) — unverified Google emails cannot
resolve to a DataSpoke account. If the credentials or the session
secret are unset, `/auth/google/{login,callback}` fails
`OAUTH_NOT_CONFIGURED`. All three are delivered as a redirect to the
`/oauth-error` page rather than a response body
([§Callback failure surface](#callback-failure-surface)).

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
