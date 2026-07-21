# DataSpoke Helm Charts

Single deployment subsystem for DataSpoke — both production and development.
Scripts live in `helm-charts/bin/`, chart values in `helm-charts/dataspoke/`, and
dev-only peripheral manifests in `helm-charts/dev-peripherals/`.

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM** (dev) or operator-specified sizing (prod)

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
- Cluster capacity for the prod resource budget in `dataspoke/values.yaml` (2
  API + 2 frontend replicas, Postgres 1-2 CPU / 2-6Gi, Airflow's five
  components, Redis primary + replica) — size nodes accordingly.

#### 2. Create the namespace and the 12-key credential Secret

For a real deployment, deliver these 12 keys via ExternalSecrets, Vault, or
SealedSecrets rather than plain `kubectl`. The `kubectl` form below is only
the floor for a one-off bootstrap: run it from a private/short-lived shell,
and prefer the `--from-env-file` variant over `--from-literal=` — the latter
lands every credential in your shell history and in `ps auxww` /
`/proc/<pid>/cmdline` for the process's lifetime, visible to any co-tenant on
the same bastion.

Generate the five high-entropy keys first (HS256/HMAC signing and
random-token values — security is entirely a function of their entropy):

```bash
openssl rand -hex 32   # run 5 times, one per high-entropy key below
```

```bash
kubectl create namespace <your-namespace>

# Write values to a file kubectl reads (not --from-literal=, which leaks into
# shell history/argv — see above), then remove the file.
cat > /tmp/dataspoke-secrets.env <<'EOF'
DATASPOKE_POSTGRES_USER=<u>
DATASPOKE_POSTGRES_PASSWORD=<p>
DATASPOKE_POSTGRES_DB=<db>
DATASPOKE_REDIS_PASSWORD=<p>
DATASPOKE_AIRFLOW_USER=<u>
DATASPOKE_AIRFLOW_PASSWORD=<p>
DATASPOKE_AIRFLOW_WEBSERVER_SECRET_KEY=<k>
DATASPOKE_AIRFLOW_JWT_SECRET=<k>
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
| `DATASPOKE_POSTGRES_USER`/`PASSWORD`/`DB`, `DATASPOKE_REDIS_PASSWORD`, `DATASPOKE_AIRFLOW_USER`/`PASSWORD` | operator-chosen | pick unique values; avoid `admin`/`postgres`-style defaults |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | externally issued | from the Google Cloud Console OAuth client |

The preflight (`_check_airflow_credentials_prod` in `bin/install.sh`, plus the
IngressClass/Secret-existence/`--image-tag` checks earlier in the same prod
branch) fails fast — before any resources are created — on any of:

| Check | Rejected when |
|-------|---------------|
| IngressClass | not found in the cluster |
| Secret | missing entirely |
| All 12 keys | any of the 12 keys above is absent |
| `DATASPOKE_JWT_SECRET_KEY` | equals the dev default `changeme-dev-secret-do-not-use-in-prod` |
| `DATASPOKE_AIRFLOW_USER` | equals `admin` |
| `DATASPOKE_AIRFLOW_PASSWORD` | empty, or equals `admin` |
| `DATASPOKE_GOOGLE_OAUTH_CLIENT_SECRET` | starts with `placeholder-` |
| `--image-tag` | not passed explicitly (prod refuses the mutable `:dev` tag) |

Note preflight validates known-bad literals only — it does not measure
entropy, so e.g. `DATASPOKE_JWT_SECRET_KEY=x` passes.

This walkthrough already uses a custom Secret name
(`dataspoke-secrets-prod`) via `secrets.existingSecret` in the overlay (§3) —
the same 12 keys and rejection rules apply to whatever Secret that name
resolves to. Set it to `dataspoke-secrets` instead (or omit
`secrets.existingSecret` from your overlay) to use the chart default name.

#### 3. Author your operator overlay

Copy `helm-charts/values-prod.example.yaml`, edit the placeholders (ingress
hosts, TLS secret names, CORS origins, OAuth post-login redirect, Google
client ID, `secrets.existingSecret`), and read its header comment on Helm's
map-merge / list-replace semantics before touching the
`cert-manager.io/cluster-issuer` annotation — the single biggest footgun for
operators not running cert-manager.

#### 4. Install

```bash
./helm-charts/bin/install.sh --profile prod \
  --values /path/to/your-overlay.yaml \
  --image-tag 1.2.3
```

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
when `ENV_FILE` is unset. It hardcodes
`<scheme>://api.<DATASPOKE_KUBE_INGRESS_DOMAIN>/internal/admin/bootstrap`, so
your overlay's API ingress host **must** equal
`api.<DATASPOKE_KUBE_INGRESS_DOMAIN>`.

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
limited to `helm-charts/.env.{dev,prod}`. `.gitignore` only ignores the three
literal names `helm-charts/.env`, `.env.dev`, and `.env.prod` — any other
filename is committable. A deployment fork can therefore commit a file like
`helm-charts/env.prod-no-credential` for reproducible team installs, as long
as it carries **no credentials**. Start the file with a header comment
stating the rule mechanically, not just descriptively:

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
and the chart-derived Secrets (`dataspoke-airflow-metadata-db`,
`dataspoke-airflow-api-secret-key`, `dataspoke-airflow-jwt-secret`), but
retains everything below by design. There is no prod `--delete-pvcs` — that
flag is dev-only; namespace deletion is prod's only full wipe. **The
uninstaller asks "delete the namespace?" first and only prints this same
summary afterward, when you answered no** (`--delete-all` also implies
`--delete-namespaces`, so it skips the summary too) — the table below is a
reference, not something you need to remember.

| Resource | Kind | Size | Notes |
|----------|------|------|-------|
| `data-dataspoke-postgresql-0` | PVC | 50Gi | Postgres primary data |
| `redis-data-dataspoke-redis-master-0` | PVC | 8Gi | Redis primary data |
| `redis-data-dataspoke-redis-replicas-0` | PVC | 8Gi | Redis replica data (StatefulSet `dataspoke-redis-replicas`, plural — only the replica name pluralizes) |
| `dataspoke-secrets` (or your `secrets.existingSecret`) | Secret | -- | operator-owned; never deleted |
| `dataspoke-airflow-fernet-key` | Secret | -- | keep-annotated by the Airflow chart; survives silently |
| `dataspoke-llm-secret`, `dataspoke-datahub-secret`, `dataspoke-langfuse-secret`, `dataspoke-smtp-secret` | Secret | -- | out-of-band, not managed by this script; survive silently if present |

**Fernet-key coupling**: if you keep the Postgres PVC, keep
`dataspoke-airflow-fernet-key` too — existing encrypted Airflow connections
are only decryptable with that same key. Deleting one without the other
breaks Airflow on the next install that reuses the PVC.

**Deleting the credential Secret**: deleting `dataspoke-secrets` (or your
`secrets.existingSecret`) destroys the only copy of all 12 credentials unless
they also live in an external secrets manager, and strands the Postgres PVC
above if you keep it — the running cluster still expects the old
`DATASPOKE_POSTGRES_PASSWORD`. Delete the Secret only together with the PVCs
above, or not at all.

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

kubectl delete secret dataspoke-secrets dataspoke-airflow-fernet-key \
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

This rebuilds the API image, runs `helm upgrade`, restarts the deployment, and
waits for rollout.

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
