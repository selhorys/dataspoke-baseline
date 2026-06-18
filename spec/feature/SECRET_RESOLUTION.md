# Secret Resolution

## Table of Contents
1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Design](#design)
4. [Data Model](#data-model)
5. [Interfaces](#interfaces)
6. [Open Questions](#open-questions)

## Overview

Ingestion recipes reach external sources with credentials, but the recipe stored in
DataSpoke (`ingestion_source.recipe`) never contains plaintext. Credentials live in
Kubernetes Secrets in DataSpoke's own namespace; the recipe references them inline, in a
DataHub-compatible form, and the backend resolves the reference at run time.

The model is **reference-only**: DataSpoke never writes credential values. An operator/admin
pre-creates the Kubernetes Secret out-of-band (`kubectl`, Terraform, External Secrets
Operator, …); DataSpoke only **lists** the available references, **verifies** they exist at
source-save time, and **resolves** them when an `ACTIVE_CUSTOM_MANAGED` extractor runs.

The reference syntax is `${name__key}`, embedded directly in `recipe.source.config` (e.g.
`password: '${team-pg__password}'`) — the same `${...}` substitution DataHub's own recipe
loader uses, so the recipe stays byte-compatible. `${name__key}` resolves to Kubernetes
Secret `dataspoke-source-cred-<name>`, data key `<key>`. Kubernetes Secrets are the **only
backend the baseline implements**; the reference itself is backend-agnostic, which is the
extension point for custom secret stores (see [Backend extensibility](#backend-extensibility)).

This spec covers the secret-naming convention, the admin authoring guide, the list/verify/
resolve flows, the RBAC model, and the error taxonomy. UC1 Ingestion Control is the first
consumer ([BACKEND §Ingestion Service](BACKEND.md#ingestion-service-srcbackendingestion));
future subsystems reuse the same module.

## Goals & Non-Goals

### Goals

- Plaintext credentials never persist in `ingestion_source.recipe` (or any DataSpoke table).
  The recipe holds only `${name__key}` references.
- Credentials live in Kubernetes Secrets in DataSpoke's own namespace. The secret resolver
  itself is read-only (`get`, `list`) — it never calls write verbs on `secrets`. The shared
  API ServiceAccount Role also carries `create`/`patch` for infra accessors; see
  [RBAC model](#rbac-model).
- The reference syntax is DataHub-recipe-compatible (`${...}`), so the recipe can be lifted to
  DataHub Managed Ingestion (or vice versa) without rewriting the secret wiring.
- The API lets users discover which references are available (`name__key`) without ever
  exposing values, and verifies a referenced secret exists when a source is saved.
- Resolution failures are non-fatal at the API layer (5xx-free) and surface as ingestion run
  errors at the appropriate point in the lifecycle.

### Non-Goals

- **Writing/vaulting credential values through DataSpoke.** Reference-only — DataSpoke has no
  create/patch path for `dataspoke-source-cred-*` Secrets. Admins author them out-of-band.
- **Custom secret-store backends in the baseline.** The baseline product implements exactly one
  backend: Kubernetes Secrets. The `${name__key}` reference is backend-agnostic, so users can
  extend DataSpoke with a resolver backed by a safer managed store (AWS Secrets Manager, GCP
  Secret Manager, HashiCorp Vault) without changing recipes or the API surface — see
  [Backend extensibility](#backend-extensibility).
- Cross-namespace references. All Secrets live in DataSpoke's own namespace. Operators needing
  a credential from another namespace replicate it into the DataSpoke namespace (ESO,
  copy-secret jobs, manual kubectl).
- **Managing orphaned secret items.** A `name__key` entry (or whole Secret) no longer referenced
  by any source is not detected, reported, or deleted by the baseline product. The model is
  reference-only and multiple sources may share a Secret, so reference counting, orphan
  reporting, and auto-deletion are all outside the implementation scope. An operator-side
  script (list `dataspoke-source-cred-*` Secrets, intersect with `${...}` references across
  active sources, delete the orphans) covers this without any runtime write verb.
- Encrypting Kubernetes Secrets at rest beyond the cluster's etcd encryption-at-rest config.
  Operators are responsible for cluster hardening.
- Client-side credential resolution. The frontend and CLI never resolve references.

## Design

### Single namespace policy

All Kubernetes Secrets accessed by DataSpoke live in the API pod's own namespace (e.g.,
`dataspoke-01` in dev). The resolver reads the namespace at startup from
`/var/run/secrets/kubernetes.io/serviceaccount/namespace`. There is no cross-namespace form.

Operators with source credentials managed elsewhere (e.g., a Terraform-managed Postgres
password in a `databases` namespace) replicate the secret into DataSpoke's namespace via ESO,
a copy-secret CronJob, or a one-time `kubectl create secret`.

### Name prefix policy

A referenceable Secret's name must start with the fixed prefix `dataspoke-source-cred-`.
The `${name__key}` reference resolves to Secret `dataspoke-source-cred-<name>` — i.e. the
prefix is implicit in the syntax and `<name>` is the part after it.

This is a security boundary, not a convention. DataSpoke's own runtime-config Secrets
(`dataspoke-secrets`, `dataspoke-airflow-metadata-db`, `dataspoke-llm-secret`, per
[HELM_CHART §Secrets Management](HELM_CHART.md#secrets-management)) live in the same namespace.
Confining resolution and listing to the `dataspoke-source-cred-` prefix means a recipe author
can never read (or even enumerate) the JWT signing key, the DataHub token, or the internal-auth
token via a reference. DataSpoke holds no write verb on Secrets at all (reference-only), so a
recipe can never mutate an infra Secret either.

`<name>` must be a DNS-label-safe segment (lowercase alphanumeric and `-`, no underscores), since
it forms part of a Kubernetes object name. `<key>` follows Kubernetes Secret data-key rules
(`[A-Za-z0-9._-]+`). Because `<name>` cannot contain `__`, the reference parser splits
unambiguously on the **last** `__` (see [resolve flow](#run-time-resolve-flow)).

### Backend extensibility

The baseline product uses **only the Kubernetes Secrets backend**. This is a product boundary,
not an interface limitation: a `${name__key}` reference names a logical credential (`name`) and
an entry within it (`key`) and carries no backend information. The neutral resolver layer
(grammar, `${...}` substitution, cache, error taxonomy, and the public functions in
[Public surface](#public-surface)) is backend-independent. A custom secret store based on safer
storage — AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault — is added by implementing
the `SecretBackend` protocol (`read_value` / `verify` / `list_refs` over the logical
`(name, key)` pair; e.g. `name` → managed-secret identifier, `key` → JSON field within it) and
binding it via `src.shared.secrets.set_backend()`. Recipes, the list/verify/resolve flows, the
error taxonomy, and the API surface stay unchanged; only the backend and its deployment wiring
(Kubernetes RBAC → cloud IAM) are swapped.

### Admin authoring guide (out-of-band)

An admin creates a source credential Secret before a source that references it is saved:

```bash
# Secret name = dataspoke-source-cred-<name>; one or more data keys.
kubectl create secret generic dataspoke-source-cred-team-pg \
  --namespace dataspoke-01 \
  --from-literal=password='<plaintext>'
```

A single Secret may hold multiple keys (e.g. `password`, `ssl_key`), and a single Secret may
back multiple sources. After creation, the reference `${team-pg__password}` is usable in any
recipe. Equivalent ESO / Terraform / sealed-secrets flows are supported as long as the
resulting object lands in DataSpoke's namespace under the prefix. Rotation is a plain
`kubectl` update of the Secret value — the recipe is untouched, and the resolver picks up the
new value within the cache TTL.

The same guidance surfaces to end users as the **authoring-guide panel** in the ingestion source
editor: a read-only helper that renders this `kubectl create secret` recipe with the in-cluster
namespace filled in, the `dataspoke-source-cred-` prefix rule, and the `${name__key}` reference
syntax, shown next to the available-reference list (`GET /spoke/ingestion/secrets`). It is
instruction only — the panel calls no write route, consistent with the reference-only model. See
[FRONTEND_INGESTION.md §Create View](FRONTEND_INGESTION.md#create-view-ingestionsourcesnew). The
`GET /spoke/ingestion/secrets` description in [API.md](../API.md) carries the same authoring
pointer for API consumers.

### Reference discovery (list flow)

`GET /spoke/ingestion/secrets` enumerates the references an author may use, **never the
values**. DataSpoke lists Secrets in its own namespace whose name starts with
`dataspoke-source-cred-`, expands each Secret's data keys, and returns one row per
`(secret, key)` pair:

```jsonc
{
  "secrets": [
    { "ref": "team-pg__password", "secret_name": "dataspoke-source-cred-team-pg", "key": "password" },
    { "ref": "team-pg__ssl_key",  "secret_name": "dataspoke-source-cred-team-pg", "key": "ssl_key" }
  ]
}
```

`ref` is the literal string an author pastes into a recipe as `${...}`. The endpoint returns
only metadata (names + keys); Secret values are never read on this path. Requires the `list`
verb on Secrets (see [RBAC model](#rbac-model)).

This endpoint requires the caller to be **Editor or Admin** (`403 READ_ONLY_ROLE` for Reader) —
a deliberate exception to the general Reader-GET rule (see
[AUTH.md §Privilege Model](AUTH.md#privilege-model)). Enumerating which credential references
exist is author/operator tooling, not general read access, so it is gated to writers even though
no values are exposed.

### Reference verify flow (at source save)

When a source is created/updated, DataSpoke extracts every `${name__key}` reference from
`recipe.source.config` and verifies each one before persisting:

0. **Prefix is implicit**: `${name__key}` always maps to `dataspoke-source-cred-<name>`. A
   reference whose `<name>` resolves to a non-prefixed or non-existent Secret fails below.
1. For each distinct `${name__key}`: parse → `(dataspoke-source-cred-<name>, <key>)`.
2. API calls k8s `read_namespaced_secret(name, own-ns)`.
3. Secret missing → `422 SECRET_REF_NOT_FOUND` (`detail`: which ref, which secret name).
4. `data[key]` missing → `422 SECRET_REF_NOT_FOUND` (`detail`: which ref, which key).
5. All references resolve → persist the source.

Verification is at save time so authors get immediate feedback. A Secret deleted between save
and run still surfaces as a run-time error — see [error taxonomy](#error-taxonomy). A
malformed reference (e.g. no `__`, empty segment) fails as `422 SECRET_REF_MALFORMED`.

### Run-time resolve flow

Before an `ACTIVE_CUSTOM_MANAGED` extractor runs, the service deep-copies the recipe and
substitutes every `${name__key}` with its plaintext value:

```
${name__key}  ─▶ split on last "__"  ─▶ (name, key)
              ─▶ secret = "dataspoke-source-cred-" + name
              ─▶ cache hit? ─yes─▶ substitute decoded value
                            │
                            no
                            ▼
                     k8s GET /api/v1/namespaces/<own-ns>/secrets/<secret>
                            ▼
                     cache + substitute data[key] (base64-decoded)
```

The resolved recipe dict (plaintext in memory only) is handed to the extractor; the stored
recipe keeps the `${name__key}` form. The resolver loads in-cluster ServiceAccount config at
first use. If that load fails, every subsequent call raises `SecretResolverUnavailable` — no
silent fallback. `DATAHUB_MANAGED` and `PASSIVE` sources are never resolved by DataSpoke
(DataHub or the external pipeline owns their secrets).

### Cache

In-memory cache in the backend-neutral resolver layer, 60s TTL, keyed on the logical
`(name, key)` pair. Bounds the k8s API call rate when a
burst of runs/dry-runs hits the same Secret. Per-process; pod restart clears it. TTL is short
enough that rotations propagate within a minute. Bounded with a hard cap (LRU eviction by
insertion order) so a long-running pod with many distinct refs cannot grow the cache without
limit.

### Error taxonomy

| Error | When | Surface |
|---|---|---|
| `SecretRefMalformed` | A `${...}` reference has no `__`, or an empty name/key segment | `422 SECRET_REF_MALFORMED` at source PUT/PATCH |
| `SecretRefNotFound` | Verify (save) failed, or run-time: Secret/key absent | `422 SECRET_REF_NOT_FOUND` at PUT/PATCH; `IngestionResult(errors=[…])` → `status="error"` at run-time |
| `SecretResolverUnavailable` | In-cluster k8s config not loadable | `503` at PUT/PATCH (cannot verify or list); `IngestionResult(errors=[…])` at run-time |
| RBAC `Forbidden` (403) from k8s API | API ServiceAccount lacks `get`/`list` on secrets | Wrapped as `SecretRefNotFound` for read; `503` for the list endpoint |
| K8s API transient errors (5xx, network) | Cluster instability | `503` at PUT/PATCH / list; `IngestionResult(errors=[…])` at run-time |

API-boundary errors (verify, list) surface as `422`/`503` with the standard DataSpoke error
envelope. Run-time resolution failures surface as `status="error"` in the ingestion run
response (200 envelope), consistent with the `sources/{id}/method/run` contract.

### RBAC model

The Helm chart adds a single `Role` in the API pod's own namespace granting `get`, `list`,
`create`, and `patch` on the `secrets` resource (`delete` intentionally omitted). A
`RoleBinding` binds the Role to the API ServiceAccount.

The Role is shared between two access patterns in the same ServiceAccount:

- **Source-cred reads** (uses `get` + `list`): `list_source_cred_refs` enumerates
  `dataspoke-source-cred-*` Secrets; `verify_secret_ref` / `resolve_secret_ref` read them.
  The resolver never issues write calls — the security property "DataSpoke does not mutate
  source-cred Secrets" is enforced in application code (the Kubernetes backend's prefix guard,
  `src/shared/secrets/k8s.py`).
- **Infra accessor writes** (uses `get` + `create` + `patch`): the admin peripheral accessors
  (`datahub_secret.py`, `llm_secret.py`, `langfuse_secret.py`, `smtp_secret.py`) use
  create-or-patch semantics against their respective fixed-name infra Secrets. The fixed
  hardcoded names (`dataspoke-datahub-secret`, `dataspoke-llm-secret`, etc.) are the
  application-level control; RBAC cannot narrow this further because `resourceNames`
  scoping per pattern would require two separate Roles.

`create` and `patch` are present in the Role because the infra accessors need them.
This does not weaken the source-cred model: the resolver module never calls write verbs,
and the prefix guard prevents a recipe reference from reaching any non-`dataspoke-source-cred-`
Secret name.

The chart exposes `Values.api.secretReader.enabled` (default `true`) to gate the entire RBAC
bundle for deployments that disable ingestion entirely. There is no
`Values.api.secretReader.namespaces[]` — single-namespace policy is enforced.

## Data Model

No DataSpoke table stores credentials. `ingestion_source.recipe` (JSONB) holds the recipe with
`${name__key}` references inline in `source.config`, e.g.:

```jsonc
{
  "source": {
    "type": "postgres",
    "config": {
      "host_port": "pg.example:5432",
      "username": "spoke_reader",
      "password": "${team-pg__password}",   // reference, never plaintext
      "env": "DEV",
      "schema_pattern": { "allow": ["^catalog$"] }
    }
  }
}
```

The resolver and the list endpoint are stateless (cache aside). No new columns, no DB
migration.

## Interfaces

### Module location

The `src/shared/secrets/` package: `interface.py` (exceptions, `SecretRefInfo`, the
`SecretBackend` protocol), `grammar.py` (the single `${name__key}` pattern and parser),
`resolver.py` (the backend-neutral cache, `${...}` substitution, backend binding, and public
functions), and `k8s.py` (the `KubernetesSecretBackend` and in-cluster client bootstrap). The
in-cluster client + TTL-cache machinery is shared with the DataHub-token / LLM-key accessors;
those consume `require_k8s_client` from `src/shared/secrets/k8s.py`, target fixed Secret names
under a separate RBAC grant, and bypass the `dataspoke-source-cred-` prefix guard under their
own fixed-name controls.

### Public surface

```python
# Resolve one reference to plaintext (run path)
def resolve_secret_ref(ref: str) -> str: ...

# Substitute every ${name__key} in a recipe dict, returning a resolved copy (run path)
def resolve_recipe_secrets(recipe: dict) -> dict: ...

# Verify a reference exists without returning its value (save path)
def verify_secret_ref(ref: str) -> None: ...

# List available references under the dataspoke-source-cred- prefix (discovery; no values)
def list_source_cred_refs() -> list[SecretRefInfo]: ...

class SecretRefMalformed(ValueError): ...
class SecretRefNotFound(LookupError): ...
class SecretResolverUnavailable(RuntimeError): ...

# Backend seam: a store implements this protocol over the logical (name, key) pair.
class SecretBackend(Protocol):
    def read_value(self, name: str, key: str) -> str: ...
    def verify(self, name: str, key: str) -> None: ...
    def list_refs(self) -> list[SecretRefInfo]: ...

def get_backend() -> SecretBackend: ...        # lazily instantiates the default backend
def set_backend(backend: SecretBackend | None) -> None: ...  # swap + clear cache; None restores default
```

The default binding is the lazily-instantiated Kubernetes backend; there is no configuration
knob to select a backend in the baseline. `set_backend()` swaps the active backend and clears
the cache; `set_backend(None)` restores the default.

All operations are synchronous. The k8s Python client's calls are blocking but fast; async
wrappers add no value over the extractor latency that dominates ingestion runs.

`resolve_secret_ref` / `verify_secret_ref` accept the `name__key` form (the inside of a
`${...}` token). The parser splits on the **last** `__` and prepends `dataspoke-source-cred-`
to the name segment; a token with no `__` or an empty segment is `SecretRefMalformed`.

### Caller integration

**At source PUT/PATCH** (`src/api/routers/spoke/ingestion.py`):

```
1. Pydantic validates the recipe shape (recipe.source.{type,config})
2. Extract all ${name__key} tokens from recipe.source.config
3. For each distinct token → verify_secret_ref(token)  (422 on malformed / not-found)
4. Persist the source with the recipe unchanged (references intact)
```

**At run time** (`src/backend/ingestion/service.py` + extractor):

```
1. Load the source; resolved = resolve_recipe_secrets(source.recipe)
2. Hand `resolved` (plaintext in memory) to the extractor for recipe.source.type
3. On any resolver exception: return IngestionResult(errors=[…]) → status="error"
```

**Discovery** (`GET /spoke/ingestion/secrets`): calls `list_source_cred_refs()` and returns the
`{secrets: [...]}` envelope above.

### API schema (`src/api/schemas/ingestion.py`)

The source recipe is a typed model; secret references are validated as strings (the resolver
owns reference semantics, not Pydantic). The list endpoint returns:

```python
class SecretRefInfo(BaseModel):
    ref: str          # "name__key" — paste into a recipe as ${ref}
    secret_name: str  # "dataspoke-source-cred-<name>"
    key: str

class SecretRefListResponse(BaseModel):
    secrets: list[SecretRefInfo]
```

There is no `auth` field and no vault request shape — both are removed in the per-source model.

### Helm chart

| Value | Default | Purpose |
|---|---|---|
| `api.secretReader.enabled` | `true` | Gates the `Role` + `RoleBinding` bundle |

The `Role` grants `get`, `list` on `secrets` in the API namespace.
`Values.api.secretReader.namespaces[]` does not exist; cross-namespace access is not supported.

Cross-reference: secret-management for DataSpoke's own infra credentials lives in
[HELM_CHART §Secrets Management](HELM_CHART.md#secrets-management). That section governs
DataSpoke's runtime config (DataHub token, internal Postgres password, etc.); this spec governs
how DataSpoke discovers and resolves *user-supplied source* credentials. The LLM API key is a
third case — a DataSpoke-owned secret read at runtime *and* rotated online through
`/admin/conf`; see [BACKEND_LLM §LLM API key](BACKEND_LLM.md).

## Open Questions

- [ ] Cache TTL configurability: hardcoded 60s. Defer until an operator reports a real need.
- [ ] RBAC health-check endpoint: a `verify_access() -> bool` the readiness probe can call to
      fail fast on RBAC misconfiguration. Worth considering after first production deploy.
