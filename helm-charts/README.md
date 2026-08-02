# DataSpoke Helm Charts

Single deployment subsystem for DataSpoke — both production and development.
Scripts live in `helm-charts/bin/`, chart values in `helm-charts/dataspoke/`, and
dev-only peripheral manifests in `helm-charts/dev-peripherals/`.

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- `python3` (Fernet key generation, values parsing — required by every profile of `bin/install.sh` and by the prod path of `bin/uninstall.sh`)
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM** (dev) or, for prod, node capacity for the umbrella chart's steady-state resource **requests** — **~4.35 CPU / 7.7 GiB RAM** across all replicas, per `spec/feature/HELM_CHART.md §Resource Sizing`'s Total row (limits burst to ~10.15 CPU / 18.1 GiB; add 66 GiB persistent volume across Postgres and Redis) — see [§Prod profile](#prod-profile), step 1, below for the per-component breakdown

## Quick Start

### Configure

```bash
cp helm-charts/.env.dev.example helm-charts/.env.dev
# Edit helm-charts/.env.dev — set DATASPOKE_KUBE_CLUSTER, DATASPOKE_KUBE_IMAGE_REGISTRY, etc.
```

### Dev profile (full install)

```bash
./helm-charts/bin/install.sh --profile dev
# Defaults to helm-charts/.env.dev; override with --env-file <path>
```

The `--frontend` flag controls how the Next.js UI is handled:

| Flag | Behavior |
|------|----------|
| `--frontend none` | Do not deploy the frontend (dev default). |
| `--frontend local` | Write `src/frontend/.env.local` pointing at the in-cluster API, then run `pnpm dev` on the host. |
| `--frontend cluster` | Build the frontend image and deploy it in-cluster. |

`--frontend local` and `--frontend cluster` are dev-only modes. In prod the default is `--frontend cluster` (always deployed).

### Health check

```bash
./helm-charts/bin/health-check.sh
```

### Uninstall

```bash
./helm-charts/bin/uninstall.sh --profile dev                       # Full teardown
./helm-charts/bin/uninstall.sh --profile dev --components frontend  # Remove only the frontend (helm upgrade frontend.enabled=false)
```

### Prod profile

A prod install runs three phases — pre-flight, image build, umbrella chart —
followed by an automatic post-install step: it seeds the default admin user
(`dataspoke@dataspoke.local` / `dataspoke`) unless `--skip-seed` is passed.
`SKIP_SEED` defaults to `false`, so **this runs on every prod install unless
you explicitly opt out**. (The `--components seed` gate you may have seen
elsewhere is dev-only — it does not guard the prod seed step.)

That default credential is published in this repository, so a login as
`dataspoke@dataspoke.local` / `dataspoke` succeeds against your API the
moment install returns, for anyone who can reach it. How reachable that is
depends entirely on your ingress controller and cluster network posture —
DataSpoke's chart applies no source-range or auth gating on the API ingress,
and the prod profile does not install or configure an ingress controller
(`DATASPOKE_KUBE_INGRESS_MODE=shared` by default — you bring your own).
Rotate the credential (§5) immediately after install regardless; treat it as
urgent if your controller is shared or internet-reachable. Follow this
runbook in order.

#### 1. Prerequisites

- Cluster-scoped prerequisites the Helm release cannot own itself — at
  minimum, any non-default `StorageClass` your operator overlay pins for
  Postgres, Redis, or Airflow persistence. Apply these first; see
  [`helm-charts/prod-prereq/`](prod-prereq/) for what belongs here and why,
  and the exact overlay keys the pre-flight checks.
- An IngressClass already installed in the cluster (default expected name
  `nginx`; override with `DATASPOKE_KUBE_INGRESS_CLASS`) — the preflight checks
  for it and fails fast if absent, and every Ingress the install creates (API,
  frontend, Airflow) binds to it. The install passes it by `--set`, which
  outranks the `className` in a `--values` overlay, so the env var is the one
  place to change it.
- DNS (or your own resolution mechanism) pointing the `app.`, `api.`, and
  `airflow.` subdomains of your chosen domain at that ingress controller.
- Registry auth for pulling the built images — either a public registry or an
  `imagePullSecret`. Today only the API workload threads one
  (`api.imagePullSecrets`, see `values-prod.example.yaml`); Postgres and
  Airflow pull from the same registry but take their pull secret via their
  own subchart-native keys (`postgresql.global.imagePullSecrets`,
  `airflow.imagePullSecrets`) — set those too if your registry requires auth.
  The frontend subchart does not support a pull secret at all yet, so a
  private-registry operator running `--frontend cluster` will see
  `ImagePullBackOff` on that one workload regardless.
- Cluster capacity for the prod resource budget in `dataspoke/values.yaml` — 2
  API + 2 frontend replicas (API: 500m/1 CPU, 512Mi/1024Mi mem each; frontend:
  250m/500m CPU, 256Mi/512Mi mem each), Postgres 1-2 CPU / 2-6Gi mem, Redis
  master **and** replica sized identically at 250m/500m CPU, 256Mi/512Mi mem
  each (a smaller replica OOMKills under a full resync while the master stays
  healthy — see `spec/feature/HELM_CHART.md §Redis memory policy`), and
  Airflow's five components (api-server, scheduler, triggerer, dag-processor,
  statsd) plus the transient `db-migrate` hook Job. Total steady-state
  request/limit: **4350m/10150m CPU, 7.7Gi/18.1Gi memory, 66Gi PV** (excludes
  event-consumer, disabled by default, and the `db-migrate` hook) — see
  `spec/feature/HELM_CHART.md §Resource Sizing → Production defaults` for the
  full per-component table. Size nodes accordingly.
- `config.trustedProxyIps` (chart default: `"127.0.0.1"` — loopback only, no
  proxy trusted). The auth rate limiter (`POST /auth/token` 10/min, `POST
  /auth/register` 5/min — the only brute-force control, there is no account
  lockout) keys on the request's client address; with the loopback-only
  default every request arriving through your ingress controller shares one
  bucket. To get real per-client limits, set it to your ingress controller's
  pod CIDR in your `--values <overlay.yaml>`, e.g.
  `trustedProxyIps: "127.0.0.1,10.4.0.0/14"` — see the comment on
  `dataspoke/values.yaml`'s `config.trustedProxyIps` for why this must name
  the controller's actual pod CIDR and never widen to `"*"` or the full
  RFC1918 space. Widening it also flips `scope["scheme"]` from the trusted
  hop's `X-Forwarded-Proto`, which changes the Google OAuth `redirect_uri`
  your API generates from `http://` to `https://` — re-verify the redirect
  URI registered in the Google Cloud Console still matches before rolling
  this out.
- `config.rateLimitPerMinute` (chart default: `120`) — max requests per
  minute per rate-limit key (the same JWT-sub-or-client-IP key `trustedProxyIps`
  above attributes). The chart render fails fast on any value that is not a
  positive integer, so a bad overlay is caught by `helm template`/`install.sh`
  rather than reaching a pod.
- A CLI that can resolve an image digest, on the PATH of whatever host runs
  `install.sh`, matching what `DATASPOKE_KUBE_CLOUD_VENDOR` names: an
  authenticated `gcloud` for `GCP`, an authenticated `aws` for `AWS`, and
  `docker` when the variable is empty or names anything else — that branch
  reads the local daemon's recorded digests, so the host must also hold the
  image it is deploying. This applies to a deploy-only CI host invoked with
  `--skip-build` against images a separate build stage already pushed, which
  on the empty-vendor branch means such a host cannot resolve at all and must
  pass `--no-digest-pin`. `install.sh` uses it to resolve each
  in-scope workload's image digest before rendering the chart, so a rebuild
  pushed to the same mutable tag still rolls the workload (see
  `spec/feature/HELM_CHART.md §Digest stamping`). Pass `--no-digest-pin` to
  skip this requirement entirely — see step 4 below for what that trades
  away.

**The namespace needs no pre-creating — with one exception.** `install.sh`'s
prod pre-flight calls `ensure_namespace` ahead of every check that touches the
cluster's contents (the `--image-tag` refusal and context selection run first), so
`DATASPOKE_KUBE_DATASPOKE_NAMESPACE` is created automatically if it does not
already exist, and no separate operator step is required for it. The
credentials Secret in step 2 below is namespace-scoped, though, and is
created *before* `install.sh` ever runs — so an operator pre-creating that
Secret must create the namespace themselves first. Only the ordering is at
stake: `ensure_namespace` is idempotent and adopts a namespace the operator
already made.

#### 2. Create the namespace and the 11-key credential Secret

For a real deployment, deliver these 11 keys via ExternalSecrets, Vault, or
SealedSecrets rather than plain `kubectl`. The `kubectl` form below is only
the floor for a one-off bootstrap: run it from a private/short-lived shell,
and prefer the `--from-env-file` variant over `--from-literal=` — the latter
lands every credential in your shell history and in `ps auxww` /
`/proc/<pid>/cmdline` for the process's lifetime, visible to any co-tenant on
the same bastion.

Generate the five high-entropy hex keys first (HS256/HMAC signing and
random-token values — security is entirely a function of their entropy):

```bash
openssl rand -hex 32   # run 5 times, one per high-entropy key below
```

`DATASPOKE_AIRFLOW_FERNET_KEY` is high-entropy too, but Fernet rejects the hex
encoding above — it requires URL-safe base64 of exactly 32 raw bytes:

```bash
python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Only run that command for a genuinely fresh install. If this namespace already
ran Airflow against a Postgres metadata DB you are keeping — for example a PVC
retained from a previous release — generating a new key instead of supplying
the one that DB was encrypted with leaves its stored connections and Variables
permanently undecryptable; see
[§Prod: what survives an uninstall](#prod-what-survives-an-uninstall) for how
to recover the live key.

```bash
kubectl create namespace <your-namespace>

# Write values to a file kubectl reads (not --from-literal=, which leaks into
# shell history/argv — see above), then remove the file.
cat > /tmp/dataspoke-secrets.env <<'EOF'
DATASPOKE_POSTGRES_PASSWORD=<p>
DATASPOKE_REDIS_PASSWORD=<p>
DATASPOKE_AIRFLOW_USER=<u>
DATASPOKE_AIRFLOW_PASSWORD=<p>
DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY=<k>
DATASPOKE_AIRFLOW_JWT_SECRET=<k>
DATASPOKE_AIRFLOW_FERNET_KEY=<f>
DATASPOKE_INTERNAL_TOKEN=<t>
DATASPOKE_JWT_SECRET_KEY=<k>
DATASPOKE_OAUTH_STATE_SECRET=<k>
DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET=<s>
EOF
chmod 600 /tmp/dataspoke-secrets.env
kubectl create secret generic dataspoke-secrets-prod \
  --from-env-file=/tmp/dataspoke-secrets.env \
  -n <your-namespace>
rm /tmp/dataspoke-secrets.env
```

Key classification — the preflight below rejects only the one known-bad
literal per key, not weak values in general; entropy and uniqueness are on
you:

| Key | Type | How to set it |
|-----|------|----------------|
| `DATASPOKE_JWT_SECRET_KEY`, `DATASPOKE_OAUTH_STATE_SECRET`, `DATASPOKE_INTERNAL_TOKEN`, `DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY`, `DATASPOKE_AIRFLOW_JWT_SECRET` | high-entropy random | `openssl rand -hex 32` |
| `DATASPOKE_AIRFLOW_FERNET_KEY` | high-entropy random, fixed shape | URL-safe base64 of 32 raw bytes — see command above, not `openssl rand -hex` |
| `DATASPOKE_POSTGRES_PASSWORD`, `DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_AIRFLOW_USER`/`PASSWORD` | operator-chosen | pick unique values; avoid `admin`/`postgres`-style defaults |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | externally issued | from the Google Cloud Console OAuth client |

`DATASPOKE_POSTGRES_USER` and `DATASPOKE_POSTGRES_DB` are **not** in this
Secret. Both are non-secret and live in the app ConfigMap instead
(`config.postgres.user`/`config.postgres.db` chart values, default
`"dataspoke"`/`"dataspoke"`) — see `helm-charts/dataspoke/values.yaml`'s
`config.postgres` comment. The role (`DATASPOKE_POSTGRES_USER` /
`config.postgres.user`) is chart-pinned: it also appears as a bare literal in
the bundled subchart's `initdb` `GRANT`/`OWNER` statements
(`primary.initdb.scripts` in both `dataspoke/values.yaml` and
`dataspoke/values-dev.yaml`) and in `install.sh`'s
`_derive_airflow_metadata_secret` — changing it is unsupported. The database
(`DATASPOKE_POSTGRES_DB` / `config.postgres.db`) has no such third site:
`initdb` runs against `postgresql.auth.database` as its default connection
and never names it as a literal, so `config.postgres.db` plus
`postgresql.auth.database` is a clean, fully-guarded two-value pair you may
change together.

**Migration note for existing prod installs.** If your credentials Secret
still carries `DATASPOKE_POSTGRES_USER` or `DATASPOKE_POSTGRES_DB` from
before this change, remove them before your next `install.sh` run — the
preflight below now rejects a Secret that still carries either key rather
than silently ignoring it:

```bash
kubectl patch secret dataspoke-secrets-prod -n <your-namespace> --type=merge \
  -p='{"data":{"DATASPOKE_POSTGRES_USER":null,"DATASPOKE_POSTGRES_DB":null}}'
```

**Rotating `DATASPOKE_POSTGRES_PASSWORD` needs a manual `ALTER ROLE` too.**
Bitnami's PostgreSQL image only sets the role's password from
`DATASPOKE_POSTGRES_PASSWORD` at first bootstrap (`initdb`) — writing a new
value into the credentials Secret and re-running `install.sh` re-derives
`dataspoke-airflow-metadata-db` with the new password and restarts the
Airflow pods that hold it (see §Secrets Management in
`spec/feature/HELM_CHART.md`), but the running PostgreSQL role's own
password is untouched. Without also running `ALTER ROLE dataspoke WITH
PASSWORD '<new-password>';` against the live database, the API's
alembic-migrate init container and every rotated consumer fail
authentication against the old password on the very next rollout.

The preflight (`_check_airflow_credentials_prod` in `bin/install.sh`, plus the
IngressClass/StorageClass/Secret-existence/`--image-tag` checks earlier in the
same prod branch) fails fast — before the chart is installed, and before any
credential-derived Secret (e.g. `dataspoke-airflow-metadata-encryption-key`)
is created or modified — on any of (note the namespace itself may already
exist by this point: `ensure_namespace` runs ahead of these checks, per
[§1. Prerequisites](#1-prerequisites) above):

| Check | Rejected when |
|-------|---------------|
| IngressClass | not found in the cluster |
| StorageClass | any class pinned in the `--values` overlay is not found in the cluster (see [§1. Prerequisites](#1-prerequisites) and `helm-charts/prod-prereq/README.md`), or a literal `-` is pinned on an Airflow key that does not honour it |
| Secret | missing entirely |
| All 11 keys | any of the 11 keys above is absent |
| `DATASPOKE_POSTGRES_USER` / `DATASPOKE_POSTGRES_DB` | either key is still present in the Secret — both moved to the app ConfigMap (`config.postgres.*`); see the migration note above |
| `DATASPOKE_JWT_SECRET_KEY` | equals the dev default `changeme-dev-secret-do-not-use-in-prod` |
| `DATASPOKE_AIRFLOW_USER` | equals `admin` |
| `DATASPOKE_AIRFLOW_PASSWORD` | empty, or equals `admin` |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | starts with `placeholder-` |
| `DATASPOKE_AIRFLOW_FERNET_KEY` shape | not URL-safe base64 of exactly 32 raw bytes (43 chars + `=`) — catches an `openssl rand -hex 32` value pasted in by mistake |
| `--image-tag` | not passed explicitly (prod refuses the mutable `:dev` tag) |

Note preflight validates known-bad literals and the Fernet key's shape only —
it does not measure entropy, so e.g. `DATASPOKE_JWT_SECRET_KEY=x` passes, and
a correctly-shaped but low-entropy Fernet key passes too. The shape check
only rejects a wrong-length value; it does not (and cannot) verify the key
actually decrypts an existing Postgres PVC's Airflow connections/Variables —
see the missing-key error's own warning against generating a new key when
one is retained.

Beyond the Secret checks above, a prod install also fails fast on
image-digest resolution (`_resolve_digest_or_abort` in `bin/install.sh`) for
the DataSpoke-owned workloads actually in scope — **api** (event-consumer
shares the api image and therefore the api digest — it has no resolution
step of its own), **frontend** (only when `--frontend cluster`) — before the
umbrella `helm upgrade` ever runs, using the vendor CLI required in [§1.
Prerequisites](#1-prerequisites) above. If that CLI is missing, or the
registry lookup itself fails (auth, network), the install aborts naming the
image reference it could not resolve — it does not silently fall back to
deploying the mutable tag. See step 4 below for `--no-digest-pin`, the
explicit escape hatch.

This walkthrough already uses a custom Secret name
(`dataspoke-secrets-prod`) via `secrets.existingSecret` in the overlay (§3) —
the same 11 keys and rejection rules apply to whatever Secret that name
resolves to. Set it to `dataspoke-secrets` instead (or omit
`secrets.existingSecret` from your overlay) to use the chart default name.

A disagreement between `DATASPOKE_AIRFLOW_FERNET_KEY` in this Secret and the
key already projected into `dataspoke-airflow-metadata-encryption-key` on a
live cluster aborts the install rather than re-projecting, and the aborting
install prints the recovery command — see
[§Prod: what survives an uninstall](#prod-what-survives-an-uninstall) for why
the guard exists and the one cycle it does not cover.

#### 3. Author your operator overlay

Copy `helm-charts/values-prod.example.yaml`, edit the placeholders (ingress
hosts, TLS secret names, CORS origins, OAuth post-login redirect, Google
client ID, `secrets.existingSecret`), and read its header comment on Helm's
map-merge / list-replace semantics before touching the
`cert-manager.io/cluster-issuer` annotation — the single biggest footgun for
operators not running cert-manager. The API ingress host is your own choice —
nothing in the install depends on it matching a fixed pattern, except
`bin/health-check.sh`: that dev-only probe tool still assumes
`api.<DATASPOKE_KUBE_INGRESS_DOMAIN>` regardless of what your prod overlay
sets. The example overlay publishes the public API surface — `/api/v1`,
`/health`, `/ready`, `/redoc`, `/openapi.json` — and never `/internal/*`; a
prod pre-flight check in `install.sh` refuses to install if any configured
path would admit `/internal/*`. `/redoc` and `/openapi.json` disclose nothing
beyond the already-public surface (the internal routers are excluded from the
schema), but the frontend's "API docs" nav link points at
`${apiBaseUrl}/redoc` — removing either path is a valid hardening step, at
the cost of that link 404ing.

#### 4. Install

`--values` is single-use — unlike helm's repeatable `-f`, it takes exactly
one overlay file and the script errors if you pass it twice. Merge multiple
overlays into one file first.

```bash
./helm-charts/bin/install.sh --profile prod \
  --values /path/to/your-overlay.yaml \
  --image-tag 1.2.3
```

This resolves each in-scope workload's image digest before rendering the
chart (see [§1. Prerequisites](#1-prerequisites) above) — `gcloud`/`aws`
(per `DATASPOKE_KUBE_CLOUD_VENDOR`) must be on this host's PATH and
authenticated against the registry, including when this same command runs
with `--skip-build` on a deploy-only CI host against images a separate build
stage already pushed. Add `--no-digest-pin` if that host cannot reach the
registry API (only `docker`/local image handling is available, or none at
all) — the three DataSpoke-owned workloads (api, event-consumer, frontend)
then deploy by mutable `<repository>:<tag>` with `imagePullPolicy: Always`,
and an explicit `kubectl rollout restart` replaces the digest-triggered
roll. `postgresql` and `airflow` are never digest-stamped regardless of this
flag — see `spec/feature/HELM_CHART.md` §Digest stamping's "airflow and
postgres are not digest-stamped" note for the manual-restart requirement
those two still carry after an image update.

This automatically seeds the default admin user
(`dataspoke@dataspoke.local` / `dataspoke`) at the end unless `--skip-seed` is
passed — see §5, which you should treat as the immediate next step.

#### 5. Rotate the auto-seeded default admin (REQUIRED — it is already live)

Step 4's install seeded `dataspoke@dataspoke.local` / `dataspoke`
automatically the moment it completed. That credential is published in this
repository, so it authenticates against your API for anyone who can reach it
— see the exposure note in §Prod profile above for how reachability depends
on your ingress controller and cluster network posture. Rotate it now, before
treating the deployment as usable by anyone other than the install operator:

```bash
TOKEN="<token from logging in as the seeded admin>"
read -r -s -p "New password: " NEW_PASSWORD; echo
curl -s -X PATCH https://api.<your-domain>/api/v1/auth/me \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{"password": "${NEW_PASSWORD}"}
EOF
```

(`read -s` plus piping the body through a heredoc into `-d @-` keeps the new
password out of both shell history and process argv — unlike embedding it
directly in a `-d '{"password": "<new-password>"}'` literal, which is visible
via `ps auxww` for the life of the curl process.)

#### 6. Re-run or skip the seed (optional)

The seed step in §4 is idempotent (safe to re-run) and only creates
`dataspoke@dataspoke.local` / `dataspoke` if no Admin exists yet — there is no
"wire DataHub before seeding" ordering constraint, user creation is
local-only. Use `--skip-seed` at install time if you provision the admin some
other way, then seed manually when ready:

```bash
ENV_FILE=helm-charts/.env.prod bash helm-charts/bin/post-install/seed-admin-user.sh
```

The `ENV_FILE=` prefix is **required** — the script defaults to `.env.dev`
when `ENV_FILE` is unset.

#### 7. Register peripherals and runtime config

```bash
curl -X PATCH https://api.<your-domain>/api/v1/admin/peripherals/datahub  -d '{...}'
curl -X PATCH https://api.<your-domain>/api/v1/admin/peripherals/langfuse -d '{...}'
curl -X PATCH https://api.<your-domain>/api/v1/admin/peripherals/smtp     -d '{...}'
curl -X PATCH https://api.<your-domain>/api/v1/admin/conf -d '{...}'
```

See `spec/API.md` for the full request/response contracts.

### Prod: committing an operator env file

`--env-file <path>` accepts any path on both `install.sh` and `uninstall.sh`
(the `--env-file` flag parsing near the top of each script) — it is not
limited to `helm-charts/.env.{dev,prod}`. `.gitignore` denies by default any
file whose basename starts with `.env` (`.env*`, unanchored — matches at any
depth, so `--env-file`'s arbitrary-path story is covered too), re-including
only `*.example` templates and `.envrc`. A filename that does NOT start with
a dot is unaffected by that rule and remains committable — a deployment fork
can commit a file like `helm-charts/env.prod-no-credential` for reproducible
team installs, as long as it carries **no credentials**. Start the file with
a header comment stating the rule mechanically, not just descriptively:

```bash
# DEPLOYMENT SHAPE ONLY. Rule: if a variable's name is not DATASPOKE_KUBE_*,
# it does not go in this file — credentials live in the pre-created
# dataspoke-secrets(-prod) K8s Secret (see §Prod profile above), never here.
# Credential-free is NOT disclosure-free: this file still names your cluster
# context, cloud project, container registry, and ingress host. Commit it
# only to a PRIVATE deployment repo.
DATASPOKE_KUBE_CLUSTER=...
DATASPOKE_KUBE_DATASPOKE_NAMESPACE=...
```

### Prod: what survives an uninstall

`./helm-charts/bin/uninstall.sh --profile prod` uninstalls the Helm release
and four chart-derived Secrets (`dataspoke-airflow-metadata-db`,
`dataspoke-airflow-api-secret-key`, `dataspoke-airflow-jwt-secret`,
`dataspoke-airflow-metadata-encryption-key`), but retains everything below by
design. These four are projections of keys held in the retained credentials
Secret, so deleting them is safe — the next install rebuilds them
byte-identically from that source.

`dataspoke-airflow-fernet-key` — the Airflow subchart's own pre-install-hook
Secret, which `helm uninstall` never removes (`hook-delete-policy:
before-hook-creation`) and which only exists on a cluster that ran a release
before `airflow.fernetKeySecretName` was pinned to
`dataspoke-airflow-metadata-encryption-key` — is deleted **conditionally**:
only when its `fernet-key` value agrees with `DATASPOKE_AIRFLOW_FERNET_KEY`
in the retained credentials Secret (a redundant copy, safe to drop). If it
disagrees, or the credentials Secret has no `DATASPOKE_AIRFLOW_FERNET_KEY` to
compare against (a Secret that predates the Fernet key joining the
credentials contract), the uninstaller
leaves it in place and warns — on such a cluster it may be the only live
carrier of the key that decrypts the retained Postgres PVC's Airflow
connections/Variables. There is no prod `--delete-pvcs` — that flag is
dev-only; namespace deletion is prod's only full wipe of the namespace's own
objects. **The uninstaller asks "delete the namespace?" first and only
prints this same summary afterward, when you answered no**
(`--delete-all` also implies `--delete-namespaces`, so it skips the summary
too) — the table below is a reference, not something you need to remember.
A namespace deletion does not by itself destroy the credential material on
these PVCs, though: under a `Retain`-reclaim-policy `StorageClass` (see
below) the underlying volumes survive the namespace and must be deleted by
hand — or their disk-encryption key destroyed — for a complete decommission.

**A custom-named Fernet Secret is untouched by `uninstall.sh`.** If this
release's Airflow chart was ever pointed at a self-chosen
`airflow.fernetKeySecretName` — for example by a direct `helm upgrade` or a
GitOps tool applying an overlay that `install.sh`'s own forced `--set` never
ran ahead of — the cleanup above only knows the two fixed names,
`dataspoke-airflow-metadata-encryption-key` (always deleted) and
`dataspoke-airflow-fernet-key` (conditionally deleted per the paragraph
above). A third, self-chosen Secret name is neither deleted nor
conditionally preserved; `uninstall.sh` does not know it exists and leaves
it exactly as it was. Locate and dispose of it by hand if you are
decommissioning the namespace for good.

| Resource | Kind | Size | Notes |
|----------|------|------|-------|
| `data-dataspoke-postgresql-0` | PVC | 50Gi | Postgres primary data |
| `redis-data-dataspoke-redis-master-0` | PVC | 8Gi | Redis primary data |
| `redis-data-dataspoke-redis-replicas-0` | PVC | 8Gi | Redis replica data (StatefulSet `dataspoke-redis-replicas`, plural — only the replica name pluralizes) |
| `dataspoke-secrets` (or your `secrets.existingSecret`) | Secret | -- | operator-owned; never deleted |
| `dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret` | Secret | -- | out-of-band, not managed by this script; survive silently if present |

Deleting one of these PVCs — by hand (see the manual commands below) or as a
side effect of a namespace deletion — does not necessarily destroy the
underlying volume: whether it does depends on the `StorageClass`'s
`reclaimPolicy`. A `Retain` reclaimPolicy leaves the volume behind, unbound
and unreclaimed, once its PVC is gone; `Delete` (the common provisioner
default) destroys it in the same action. That distinction matters here
because these volumes hold the credential store on the Postgres PVC
(password hashes, `api_tokens`/`password_reset_tokens`, Fernet-encrypted
ingestion secrets) and Redis's AOF (refresh-token revocation keys). See
[`helm-charts/prod-prereq/` §StorageClass](prod-prereq/#storageclass--the-first-case)
for the `Retain`-vs-`Delete` trade-off and an example manifest.

**Fernet key ↔ Postgres PVC coupling**: Airflow encrypts connection secrets
and Variables in its metadata DB with the Fernet key it reads from
`dataspoke-airflow-metadata-encryption-key`, projected from
`DATASPOKE_AIRFLOW_FERNET_KEY` in the retained credentials Secret. Because
that metadata lives in the retained Postgres PVC, the credentials Secret and
the PVC must be kept or dropped together.

Two different guarantees apply depending on what you do next:

- **Re-running `install.sh` against this still-live release** (no uninstall in
  between) compares `DATASPOKE_AIRFLOW_FERNET_KEY` in the credentials Secret
  against the live `dataspoke-airflow-metadata-encryption-key` projection and
  aborts on a disagreement instead of silently re-projecting it. Recover the
  live value with:

  ```bash
  kubectl get secret dataspoke-airflow-metadata-encryption-key -n <your-namespace> \
    -o jsonpath='{.data.fernet-key}' | base64 --decode
  ```

  then set `DATASPOKE_AIRFLOW_FERNET_KEY` in the credentials Secret to that
  value before retrying.
- **A full uninstall/reinstall cycle** carries a weaker version of this
  protection: this teardown always deletes `dataspoke-airflow-metadata-
  encryption-key` (it is safely regenerable, byte-identically, from the
  retained credentials Secret), removing the comparison point the guard above
  reads. Whether a comparison still happens on the next install depends on
  `dataspoke-airflow-fernet-key` (see above) — if it agreed with the
  credentials Secret at teardown time it was deleted too, and the next
  install trusts `DATASPOKE_AIRFLOW_FERNET_KEY` in the retained credentials
  Secret unchecked; if it disagreed (or the Secret had no key to compare) it
  was left in place, and the next install's `_ensure_airflow_fernet_secret`
  falls back to comparing against it, aborting on a mismatch exactly as in
  the still-live-release case above. Either way, do not edit that key by hand
  between teardown and reinstall while the Postgres PVC survives.

**Deleting the credential Secret**: deleting `dataspoke-secrets` (or your
`secrets.existingSecret`) destroys the only copy of all 11 credentials unless
they also live in an external secrets manager, and strands the Postgres PVC
above if you keep it — the running cluster still expects the old
`DATASPOKE_POSTGRES_PASSWORD` and `DATASPOKE_AIRFLOW_FERNET_KEY`. Delete the
Secret only together with the PVCs above, or not at all.

**Airflow log PVCs**: `logs-dataspoke-airflow-scheduler-0` and
`logs-dataspoke-airflow-triggerer-0` exist only if your overlay enables
Airflow log persistence (see `values-prod.example.yaml` §Airflow log
persistence) — disabled in the shipped chart default, so most installs never
create them. When present they hold real post-mortem task logs by design;
delete them only once you no longer need that retained history.

To delete manually (probe first — a PVC not present is normal for
log-persistence-off installs):

```bash
kubectl delete pvc data-dataspoke-postgresql-0 \
  redis-data-dataspoke-redis-master-0 \
  redis-data-dataspoke-redis-replicas-0 \
  -n <your-namespace>

# Only if present (see "Airflow log PVCs" above):
kubectl delete pvc logs-dataspoke-airflow-scheduler-0 \
  logs-dataspoke-airflow-triggerer-0 \
  -n <your-namespace>

kubectl delete secret dataspoke-secrets \
  -n <your-namespace>
```

Or delete the namespace for a full wipe — the sanctioned prod full teardown:

```bash
./helm-charts/bin/uninstall.sh --profile prod --delete-namespaces
```

---

## Ingress Endpoints

All HTTP services are accessed via virtual-host routing on the ingress
controller (`<SCHEME>://<service>.<INGRESS_DOMAIN>/`); TCP services (databases,
brokers) are exposed on dedicated ports and never take the scheme. How the
controller, `<INGRESS_DOMAIN>`, `<SCHEME>`, and `<TCP_HOST>` are provided
depends on `DATASPOKE_KUBE_INGRESS_MODE` in `helm-charts/.env.dev`:

- **`managed`** (default) — the install owns an nginx-ingress controller.
  `<INGRESS_DOMAIN>` auto-derives to `<LoadBalancer-IP>.nip.io` (wildcard DNS,
  no `/etc/hosts` entries) and `<TCP_HOST>` is that LoadBalancer IP (TCP
  passthrough on the controller). `<SCHEME>` is `http` (typical).
- **`shared`** — the install reuses the cluster's pre-existing ingress
  controller (e.g. AWS/EKS). The operator pre-sets
  `DATASPOKE_KUBE_INGRESS_DOMAIN` — wildcard DNS, or a record for each
  `<service>.` host in the table below (`app.`, `api.`, `airflow.`, `datahub.`,
  `datahub-gms.`, `langfuse.`) — and `<TCP_HOST>` is `127.0.0.1` — TCP
  services are forwarded to the same ports via `./helm-charts/bin/port-forward.sh`.
  `<SCHEME>` is `http` or `https` per `DATASPOKE_KUBE_INGRESS_SCHEME` — set
  `https` when the shared controller terminates TLS + HSTS in front of the
  virtual hosts, since an `http` page would break under browser
  mixed-content/auto-upgrade. `DATASPOKE_KUBE_INGRESS_TLS_SECRET` (optional)
  additionally puts a `tls:` block on the three dev ingresses DataSpoke owns
  (API, frontend, Airflow) — leave it empty when the controller terminates TLS
  with a controller-level or wildcard cert.

| Service | Address | Credentials |
|---------|---------|-------------|
| DataHub UI | `<SCHEME>://datahub.<INGRESS_DOMAIN>/` | `datahub` / `datahub` |
| DataHub GMS | `<SCHEME>://datahub-gms.<INGRESS_DOMAIN>/` | -- |
| DataSpoke Web UI (dev `--frontend cluster`) | `<SCHEME>://app.<INGRESS_DOMAIN>/` | `dataspoke` / `dataspoke` — rotate via `PATCH /api/v1/auth/me` before production (see §Prod profile §5) |
| DataSpoke Web UI (dev `--frontend local`) | `http://localhost:3000` | same as above |
| DataSpoke API | `<SCHEME>://api.<INGRESS_DOMAIN>/api/v1/` | per `.env` JWT |
| Airflow UI | `<SCHEME>://airflow.<INGRESS_DOMAIN>/` | `admin` / `admin` (see `.env`) |
| Langfuse UI | `<SCHEME>://langfuse.<INGRESS_DOMAIN>/` | `DATASPOKE_DEV_LANGFUSE_INIT_USER_{EMAIL,PASSWORD}` in `helm-charts/.env.dev` (auto-generated on first install) |
| DataSpoke PostgreSQL | `<TCP_HOST>:9201` | per `.env` |
| Redis | `<TCP_HOST>:9202` | per `.env` |
| DataHub Kafka | `<TCP_HOST>:9005` | -- |
| Example PostgreSQL | `<TCP_HOST>:9102` | `postgres` / `ExampleDev2024!` |
| Example Kafka | `<TCP_HOST>:9104` | -- |
| Lock API | `<TCP_HOST>:9221` | -- |

`datahub-gms.<INGRESS_DOMAIN>` is a credential-bearing origin: every call to it
carries the DataHub personal access token in an `Authorization: Bearer` header.
Give it the same treatment as the DataHub frontend host, not just a DNS record —
certificate SAN coverage (a wildcard cert covers it; an explicit-SAN cert must
list it) and whatever WAF or source-range allow-list your other DataSpoke hosts
sit behind. With `DATASPOKE_KUBE_INGRESS_SCHEME=https` the GMS Ingress refuses
the plaintext hop (`ssl-redirect: "true"`), so a missing SAN surfaces as a TLS
error rather than a silent downgrade.

Upgrading an existing dev environment: `DATASPOKE_TEST_DATAHUB_GMS_URL` in
`helm-charts/.env.dev` is only rewritten by the DataHub install step. Until you
re-run it, that variable still names the old origin and tooling keeps sending
the PAT there:

```bash
./helm-charts/bin/install.sh --profile dev --components datahub
```

The dev default (`--frontend none`) does not deploy the frontend pod. To run the UI on the host:

```bash
# 1. Write src/frontend/.env.local pointing at the in-cluster API
./helm-charts/bin/install.sh --profile dev --frontend local

# 2. Start the Next.js dev server
pnpm -C src/frontend install && pnpm -C src/frontend dev
# Open http://localhost:3000  —  login: dataspoke@dataspoke.local / dataspoke
```

To deploy the containerised frontend in-cluster instead:

```bash
./helm-charts/bin/install.sh --profile dev --frontend cluster
# Open <SCHEME>://app.<INGRESS_DOMAIN>/  —  login: dataspoke@dataspoke.local / dataspoke
```

The `--components frontend` fast path (rebuild + redeploy only the frontend pod) remains available as a code-iteration shortcut.

---

## Profile Differences

See `spec/feature/HELM_CHART.md §Profiles` for the canonical comparison table.
The short version: dev installs nginx-ingress, DataHub, Langfuse, dummy data,
and the dev-lock service; prod installs only the umbrella chart and assumes the
operator brings peripherals and an ingress controller.

---

## Stopping the API for Iteration

To stop the API without tearing down the full stack:

```bash
kubectl scale deployment/dataspoke-api --replicas=0 \
  -n "${DATASPOKE_KUBE_DATASPOKE_NAMESPACE}"
```

To rebuild and redeploy:

```bash
./helm-charts/bin/install.sh --profile dev --components api
```

This rebuilds the API image, runs `helm upgrade` (which pins the rebuilt image
by digest so it rolls the deployment by construction — see
`spec/feature/HELM_CHART.md` §Digest stamping), and waits for rollout.

---

## Selective Reinstall

Install (or reinstall) a subset of components:

```bash
# Reinstall DataHub only
./helm-charts/bin/install.sh --profile dev --components datahub

# Reinstall DataSpoke infra (Postgres, Redis, Airflow, API, event-consumer)
./helm-charts/bin/install.sh --profile dev --components dataspoke-infra

# Resume an interrupted full install starting at a specific component
./helm-charts/bin/install.sh --profile dev --from-component langfuse
```

Component names: `nginx-ingress`, `datahub`, `langfuse`, `dataspoke-infra`,
`api`, `frontend`, `dummy-data`, `dev-lock`, `seed`.

---

## Lock Service HTTP API

The dev-lock service provides an advisory mutex for coordinating multi-tester
access to shared dev-env resources.

`install.sh` auto-populates `DATASPOKE_TEST_LOCK_URL` in `helm-charts/.env.dev`
(`http://<INGRESS_IP>:9221` in managed mode, `http://127.0.0.1:9221` via
`bin/port-forward.sh` in shared mode), so the same command works in both:

```bash
LOCK_URL=$(grep DATASPOKE_TEST_LOCK_URL helm-charts/.env.dev | cut -d= -f2)
curl -s -X POST ${LOCK_URL}/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"owner": "alice", "message": "running ingestion test"}'
```

| Endpoint | Method | Response |
|----------|--------|----------|
| `/lock` | GET | Current lock status |
| `/lock/acquire` | POST | `200` acquired, `409` held by another, `400` missing owner |
| `/lock/release` | POST | `200` released, `403` wrong owner |
| `/lock` | DELETE | Force-release (admin) |

Lock state is in-memory and resets on pod restart.

---

## Troubleshooting

See `spec/feature/HELM_CHART.md §Troubleshooting` for the full list of known
issues and mitigations (pod eviction, OpenSearch OOM, MAE consumer stall, etc.).

Reinstall a failing component:

```bash
./helm-charts/bin/install.sh --profile dev --components <name>
```
