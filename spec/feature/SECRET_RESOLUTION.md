# Secret Resolution

## Table of Contents
1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Design](#design)
4. [Data Model](#data-model)
5. [Interfaces](#interfaces)
6. [Open Questions](#open-questions)

## Overview

Configs that hold credentials (today: `ingestion_configs.auth`; later: any subsystem that
reaches an external source) never persist plaintext passwords in the DataSpoke database.
Credentials live in Kubernetes Secrets in DataSpoke's own namespace; configs hold a
structured reference (`secret_ref: {name, key}`) that the backend resolves at credential-use
time.

The API surface accepts two write shapes:

1. **Vault path** — caller submits `{username, password, secret_ref: {name, key,
   force_overwrite?}}`. The API writes the password to a Kubernetes Secret in its own
   namespace, then persists the reference (plaintext password is dropped before DB write).
2. **Reference path** — caller submits `{username, secret_ref: {name, key}}` pointing at a
   Secret they pre-provisioned (Terraform, ESO, manual `kubectl`, etc.). The API verifies
   the Secret + key exist at PUT time and persists the reference.

Password-only requests (`{username, password}` with no `secret_ref`) are rejected with 422
to enforce the no-plaintext-in-DB invariant.

This spec covers the API contract, the validator matrix, the vault-write and read flows,
the RBAC model, and the error taxonomy. UC1 Ingestion Control is the first consumer
([BACKEND §Ingestion Service](BACKEND.md#ingestion-service-srcbackendingestion)); future
subsystems reuse the same module.

## Goals & Non-Goals

### Goals

- Plaintext credentials never persist in `ingestion_configs.auth` (or any future config
  table).
- Credentials live in Kubernetes Secrets in DataSpoke's own namespace, with bounded RBAC
  (`get`, `create`, `patch`) on `secrets` resources in that namespace only.
- Caller controls Secret naming and key — no auto-generated names. Predictable from
  outside.
- Validator at the API boundary makes invalid `auth` shapes explicit (typed errors, clear
  422 messages).
- Resolution failures are non-fatal at the API layer (5xx-free) and surface as ingestion
  run errors at the appropriate point in the lifecycle.

### Non-Goals

- Pluggable secret backends (Vault, AWS Secrets Manager, GCP Secret Manager). Future work;
  the resolver/writer interface is shaped to allow it but the baseline ships only the
  Kubernetes Secret backend.
- Cross-namespace `secret_ref`. All Secrets live in DataSpoke's own namespace. Operators
  needing a credential from another namespace replicate the secret into the DataSpoke
  namespace (via ESO, copy-secret jobs, or manual kubectl).
- Auto-deleting Kubernetes Secrets when an ingestion config is deleted. Multiple configs
  may share a Secret; reference counting is out of scope. See [Open Questions](#open-questions).
- Encrypting Kubernetes Secrets at rest beyond what the cluster's etcd encryption-at-rest
  config provides. Operators are responsible for cluster hardening.
- Client-side credential resolution. The frontend and CLI never resolve `secret_ref`.

## Design

### Single namespace policy

All Kubernetes Secrets accessed by DataSpoke live in the API pod's own namespace
(e.g., `dataspoke-01` in dev). The resolver reads the namespace at startup from
`/var/run/secrets/kubernetes.io/serviceaccount/namespace`. There is no cross-namespace
form; the parser rejects anything that looks like one.

This is a deliberate simplification over the prior cross-ns design:
- One Role grants `get/create/patch secrets` in the API namespace; no per-target-namespace
  RoleBinding loop in the Helm chart.
- Operators with sources whose secrets are managed elsewhere (e.g., a Terraform-managed
  Postgres password in a `databases` namespace) replicate the secret into DataSpoke's
  namespace via ESO, a copy-secret CronJob, or a one-time `kubectl create secret`.

### Name prefix policy

Both the vault path and the reference path require `secret_ref.name` to start with the
fixed prefix `dataspoke-source-cred-`. Names not matching the prefix are rejected with 422
`SecretRefNameForbidden` at the API boundary.

This is a security boundary, not a convention: DataSpoke's own runtime config Secrets
(`dataspoke-secrets`, `dataspoke-internal-auth`, `dataspoke-postgres-secret`,
`dataspoke-redis-secret`, etc., per [HELM_CHART §Secrets Management](HELM_CHART.md#secrets-management))
live in the same namespace and are writable under the `Role`'s `create/patch` verbs.
Without the prefix constraint, any caller authorised to PUT an ingestion conf could
overwrite the JWT signing key, the DataHub token, or the internal-auth token via the
vault path — a privilege escalation. The prefix shrinks the writer's effective surface
to Secrets DataSpoke itself owns by convention.

Operators with externally-managed source credentials (Terraform, ESO, manual `kubectl`)
must place those Secrets under the prefix, e.g., rename `team-pg-prod` to
`dataspoke-source-cred-team-pg-prod`, or have ESO sync into the prefix.

Defense-in-depth: the Helm `Role`'s `resourceNames` may also be constrained to a prefix
where Kubernetes supports it. Note that `resourceNames` does not constrain `create`
([k8s authorization reference](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources)),
so the prefix is enforced primarily at the writer (application layer); RBAC
`resourceNames` adds a second layer for `get`/`patch` only.

### `auth` request shape

The `auth` field on `PUT/PATCH ingestion/conf` accepts these structured forms:

```jsonc
// Vault path — API writes the Secret, then persists the reference
{
  "username": "<user>",
  "password": "<plaintext>",
  "secret_ref": {
    "name": "<k8s-secret-name>",
    "key": "<key-within-secret>",
    "force_overwrite": false  // optional, default false
  }
}

// Reference path — caller pre-provisioned the Secret
{
  "username": "<user>",
  "secret_ref": {
    "name": "<k8s-secret-name>",
    "key": "<key-within-secret>"
  }
}
```

`secret_ref.force_overwrite` is meaningful only on the vault path; on the reference path
it is silently ignored.

### Validation matrix (API boundary, returns 422 INVALID_PARAMETER)

| Request `auth` | Outcome |
|---|---|
| `{username}` only | 422 — no credential supplied (must include `secret_ref` for credentialed platforms) |
| `{username, password}` only (no `secret_ref`) | 422 — plaintext-only is banned |
| `{username, password, secret_ref: {name, key}}` | Vault path. 422 on `(name, key)` collision unless `force_overwrite=true` |
| `{username, password, secret_ref: {name, key, force_overwrite: true}}` | Vault path with merge-patch on existing Secret |
| `{username, secret_ref: {name, key}}` | Reference path. 422 if the Secret or key does not exist |
| `{username, password, secret_ref: {name}}` (missing `key`) | 422 — `secret_ref.key` required |
| `{username, password, secret_ref: {key}}` (missing `name`) | 422 — `secret_ref.name` required |
| Any shape with `secret_ref` as a string (legacy) | 422 — must be an object |
| `secret_ref.name` not matching prefix `dataspoke-source-cred-` | 422 `SecretRefNameForbidden` (both paths) |

For sources that do not require auth (e.g., Kafka with `NoAuth`), `auth` may be omitted
entirely. Per-platform requirements are enforced by `validate_platform_fields()`.

### Vault-write flow

0. **Prefix check**: `secret_ref.name` must start with `dataspoke-source-cred-`. If not, return
   422 `SecretRefNameForbidden`. Enforced in the validator alongside step 1.
1. **Validator** (Pydantic + `model_validator`) enforces the matrix above.
2. **Collision check**: API calls k8s `read_namespaced_secret(name, own-ns)`. If the Secret
   exists and contains `data[key]`, and `force_overwrite=false`, return 422 `SecretCollision`.
3. **Write**:
   - Secret does not exist → `create_namespaced_secret` with `data: {key: base64(password)}`.
   - Secret exists, target `key` absent from `data` → `patch_namespaced_secret` (merge-patch)
     to add `data[key]`. `force_overwrite` is irrelevant here — there is nothing to
     overwrite.
   - Secret exists, target `key` present, `force_overwrite=true` → `patch_namespaced_secret`
     with merge-patch setting only `data[key]`. Other keys in the Secret are preserved.
4. **Persist**: rewrite the auth dict to reference shape (`{username, secret_ref: {name,
   key}}`) and persist to `ingestion_configs.auth`. The plaintext `password` is dropped
   before DB write.
5. **Response**: API returns the conf with the rewritten reference shape — caller sees
   their password was vaulted.

If step 3 fails (e.g., k8s API transient error), the conf is not persisted; the API
returns 500/503 and the caller retries. There is no DB row for the failed PUT.

If step 4 fails (DB error after k8s write succeeded), the Secret is left in place. The
caller's next PUT either references the existing Secret (without `password`) or vaults
again with `force_overwrite=true`. Net effect: a possibly-orphan Secret with no DB row;
acceptable trade-off vs. the complexity of two-phase commit.

### Reference-path verify flow

On `{username, secret_ref: {name, key}}` PUT/PATCH:

0. Prefix check: `secret_ref.name` must start with `dataspoke-source-cred-`. If not, return 422
   `SecretRefNameForbidden`.
1. API calls k8s `read_namespaced_secret(name, own-ns)`.
2. Secret missing → 422 `SecretRefNotFound: secret '<name>' does not exist`.
3. `data[key]` missing → 422 `SecretRefNotFound: key '<key>' not present in secret '<name>'`.
4. Persist auth dict as-is.

Verification is at PUT time, not run time. Run-time still calls `resolve_secret_ref()`,
but with a Secret that was confirmed reachable at registration. (A Secret deleted between
PUT and run time still surfaces as a run-time error — see error taxonomy below.)

### Run-time read flow

```
auth.secret_ref ─▶ build "k8s-secret/<own-ns>/<name>/<key>"
                ─▶ cache hit? ─yes─▶ return decoded value
                              │
                              no
                              ▼
                       k8s GET /api/v1/namespaces/<own-ns>/secrets/<name>
                              │
                              ▼
                       cache + return data[key] (base64-decoded)
```

The resolver loads in-cluster ServiceAccount config at first use. If that load fails,
every subsequent call raises `SecretResolverUnavailable` — no silent fallback.

### Cache

In-memory cache, 60s TTL, keyed on `(name, key)` (namespace is implicit own-ns). Bounds
the k8s API call rate when a burst of dry-runs hits the same Secret. Per-process; pod
restart clears it. TTL is short enough that secret rotations propagate within a minute.
Bounded with a hard cap (LRU eviction by insertion order) so a long-running pod with
many distinct refs cannot grow the cache without limit.

### Error taxonomy

| Error | When | Surface |
|---|---|---|
| `SecretRefMalformed` | Validator: `secret_ref` shape wrong (non-object, missing fields, empty values) | 422 INVALID_PARAMETER at PUT/PATCH |
| `SecretRefNameForbidden` | `secret_ref.name` does not start with `dataspoke-source-cred-` | 422 INVALID_PARAMETER at PUT/PATCH |
| `SecretCollision` | Vault path: target `(name, key)` already exists and `force_overwrite=false` | 422 INVALID_PARAMETER at PUT/PATCH |
| `SecretRefNotFound` | Reference-path verify failed; or run-time: Secret/key disappeared between PUT and run | 422 at PUT/PATCH; `IngestionResult(errors=[…])` → `status="error"` at run-time |
| `SecretResolverUnavailable` | In-cluster k8s config not loadable | 503 at PUT/PATCH (cannot verify or vault); `IngestionResult(errors=[…])` at run-time |
| RBAC `Forbidden` (403) from k8s API | API ServiceAccount lacks the required verb | Wrapped to match the failing operation (`SecretRefNotFound` for read; 503 for write) |
| K8s API transient errors (5xx, network) | Cluster instability | 503 at PUT/PATCH; `IngestionResult(errors=[…])` at run-time |

API-boundary errors (vault path, reference verify) surface as 422/503 with the standard
DataSpoke error envelope. Run-time resolution failures surface as `status="error"` in the
ingestion run response (200 envelope), consistent with the existing `method/run` contract.

### Precedence

The vault-vs-reference distinction is determined by the presence of `password` in the
request body:
- `password` present → vault path (always writes the Secret first)
- `password` absent → reference path (verify-only)

Sending `password` along with a `secret_ref` that already exists, without
`force_overwrite=true`, is the collision case — explicit 422.

### RBAC model

The Helm chart adds a single `Role` in the API pod's own namespace, granting `get`,
`create`, and `patch` (no `delete`) on the `secrets` resource (`resourceNames` unset, so
any secret name in that namespace). A `RoleBinding` binds the Role to the API
ServiceAccount.

`delete` is intentionally omitted: ingestion-config DELETE does not remove the underlying
Secret (see Non-Goals; reference counting required, deferred). The chart exposes
`Values.api.secretReader.enabled` (default `true`) to gate the entire RBAC bundle for
deployments that disable ingestion entirely.

There is no `Values.api.secretReader.namespaces[]` — single-namespace policy is
enforced.

### Backwards compatibility

There is no production traffic on `ingestion_configs` yet. Prior rows persisted under the
placeholder `secret_ref` design are deleted by the test-mode reset
(`uv run python -m tests.integration.util --reset-seed`). No migration script is provided.

Calls referencing the old string-form `secret_ref: "k8s-secret/<name>/<key>"` return 422
on PUT/PATCH (validator rejects non-object). Callers update to the structured form.

## Data Model

`ingestion_configs.auth` (JSONB) holds one of three shapes after this change:

```jsonc
// No-auth source (e.g., kafka NoAuth)
null

// Reference shape (the only persisted form when auth is present)
{
  "username": "<user>",
  "secret_ref": {
    "name": "<k8s-secret-name>",
    "key": "<key>"
  }
}
```

The vault-path request shape (with `password` and optional `force_overwrite`) is **never
persisted** — it is rewritten to the reference shape before the DB write.

No new columns. No DB migration. The resolver and writer are stateless (cache aside).

## Interfaces

### Module location

`src/backend/ingestion/secret_resolver.py` (initial home; relocate under
`src/shared/secrets/` when a second subsystem becomes a consumer).

The LLM API key accessor ([`BACKEND_LLM.md` §LLM API key](BACKEND_LLM.md)) is a
second consumer of the in-cluster client + TTL-cache machinery. It reuses that
machinery but targets the fixed Secret `dataspoke-llm-secret` and is gated by the
`admin` group, so it bypasses the source-cred prefix guard (the fixed target name
plus admin auth are its controls). When that lift happens, the shared client/cache
primitives move to `src/shared/secrets/` and both consumers import them.

### Public surface

```python
# Resolver (read path)
def resolve_secret_ref(ref: str) -> str: ...

# Writer (vault path)
def write_secret_value(name: str, key: str, value: str, force_overwrite: bool) -> None: ...

# Verifier (reference path)
def verify_secret_ref(name: str, key: str) -> None: ...

class SecretRefMalformed(ValueError): ...
class SecretRefNameForbidden(ValueError): ...
class SecretRefNotFound(LookupError): ...
class SecretCollision(ValueError): ...
class SecretResolverUnavailable(RuntimeError): ...
```

All three operations are synchronous. The k8s Python client's calls are blocking but
fast; async wrappers add no value over the downstream extractor latency that already
dominates ingestion runs.

`resolve_secret_ref` accepts only the `k8s-secret/<name>/<key>` form — exactly two
segments after the `k8s-secret/` prefix, with the namespace implicit (own-ns). A
three-segment tail (`k8s-secret/<ns>/<name>/<key>`, the legacy cross-namespace form)
is rejected as `SecretRefMalformed`.

### Caller integration

**At PUT/PATCH** (`src/api/routers/spoke/common/data/ingestion.py`):

```
1. Pydantic validates auth shape (matrix above)
2. If password present → call write_secret_value(name, key, password, force_overwrite)
3. Else → call verify_secret_ref(name, key)
4. Rewrite request.auth to reference shape (drops password)
5. Persist conf
```

**At run time** (`src/backend/ingestion/extractors.py`):

```
1. Read auth.secret_ref.name and auth.secret_ref.key
2. Build "k8s-secret/<name>/<key>" string
3. Call resolve_secret_ref()
4. Use the returned plaintext as the password
5. On any resolver exception: return IngestionResult(errors=[…])
```

### API schema (`src/api/schemas/ingestion.py`)

The `auth` field becomes a typed Pydantic model rather than `dict[str, Any]`:

```python
class SecretRefSpec(BaseModel):
    name: str
    key: str
    force_overwrite: bool = False  # ignored on reference path

class AuthSpec(BaseModel):
    username: str
    password: str | None = None
    secret_ref: SecretRefSpec | None = None

    @model_validator(mode="after")
    def enforce_matrix(self) -> "AuthSpec":
        # implements the validation matrix
        ...
```

The `dict[str, Any]` form is retained at the persistence layer (`ingestion_configs.auth`
JSONB) but the API surface is typed. The persisted form is always the reference shape.

### Helm chart

| Value | Default | Purpose |
|---|---|---|
| `api.secretReader.enabled` | `true` | Gates the entire `Role` + `RoleBinding` bundle |

The `Role` grants `get`, `create`, `patch` on `secrets` in the API namespace.
`Values.api.secretReader.namespaces[]` is removed; cross-namespace access is no longer
supported.

Cross-reference: secret-management for DataSpoke's own infra credentials lives in
[HELM_CHART §Secrets Management](HELM_CHART.md#secrets-management). That section governs
DataSpoke's runtime config (DataHub token, internal Postgres password, etc.); this spec
governs how DataSpoke vaults and resolves *user-supplied source* credentials. The LLM API
key is a third case — a DataSpoke-owned secret that is read at runtime *and* rotated online
through `/admin/conf`; see [BACKEND_LLM §LLM API key](BACKEND_LLM.md).

## Open Questions

- [ ] Auto-cleanup on conf DELETE: should ingestion-config DELETE remove the underlying
      Secret? Requires reference counting (a Secret may back multiple confs). Future
      work; baseline leaves Secrets in place. An operator-side cleanup script (list all
      `dataspoke-source-cred-*` Secrets, intersect with active confs, delete the orphans) would
      cover this without burdening the runtime API.
- [ ] Pluggable backends (Vault, AWS Secrets Manager, GCP Secret Manager): the resolver
      and writer are sized for one interface. When a second backend is needed, lift the
      module under `src/shared/secrets/` and dispatch on a `SecretBackend` enum.
- [ ] Cache TTL configurability: hardcoded 60s. Defer until an operator reports a real
      need.
- [ ] RBAC health check endpoint: a `verify_access() -> bool` that the readiness probe
      can call to fail fast on RBAC misconfiguration. Worth considering after first
      production deploy uncovers operational pain.
