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

```bash
./helm-charts/bin/install.sh --profile prod \
  --values /path/to/operator-overlay.yaml \
  --image-tag 1.2.3
```

---

## Ingress Endpoints

All HTTP services are accessed via virtual-host routing on the nginx-ingress
LoadBalancer IP (`DATASPOKE_KUBE_INGRESS_IP`). TCP services (databases, brokers)
are exposed on dedicated ports.

| Service | Address | Credentials |
|---------|---------|-------------|
| DataHub UI | `http://datahub.<INGRESS_IP>.nip.io/` | `datahub` / `datahub` |
| DataHub GMS | `http://datahub.<INGRESS_IP>.nip.io/gms/` | -- |
| DataSpoke Web UI (dev `--frontend cluster`) | `http://app.<INGRESS_IP>.nip.io/` | `dataspoke` / `dataspoke` — rotate via `PATCH /auth/me` before production |
| DataSpoke Web UI (dev `--frontend local`) | `http://localhost:3000` | same as above |
| DataSpoke API | `http://api.<INGRESS_IP>.nip.io/api/v1/` | per `.env` JWT |
| Airflow UI | `http://airflow.<INGRESS_IP>.nip.io/` | `admin` / `admin` (see `.env`) |
| Langfuse UI | `http://langfuse.<INGRESS_IP>.nip.io/` | `DATASPOKE_DEV_LANGFUSE_INIT_USER_{EMAIL,PASSWORD}` in `helm-charts/.env.dev` (auto-generated on first install) |
| DataSpoke PostgreSQL | `<INGRESS_IP>:9201` | per `.env` |
| Redis | `<INGRESS_IP>:9202` | per `.env` |
| DataHub Kafka | `<INGRESS_IP>:9005` | -- |
| Example PostgreSQL | `<INGRESS_IP>:9102` | `postgres` / `ExampleDev2024!` |
| Example Kafka | `<INGRESS_IP>:9104` | -- |
| Lock API | `<INGRESS_IP>:9221` | -- |

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
# Open http://app.<INGRESS_IP>.nip.io/  —  login: dataspoke@dataspoke.local / dataspoke
```

The `--components frontend` fast path (rebuild + redeploy only the frontend pod) remains available as a code-iteration shortcut.

Replace `<INGRESS_IP>` with the value of `DATASPOKE_KUBE_INGRESS_IP` from
`helm-charts/.env.dev`. The `nip.io` suffix provides automatic wildcard DNS
resolution — no `/etc/hosts` entries needed.

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

To rebuild and redeploy (replaces the former `dataspoke-test-mode.sh`):

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

# Reinstall DataSpoke infra (Postgres, Redis, Airflow, API)
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

`install.sh` auto-populates `DATASPOKE_LOCK_URL` in `helm-charts/.env.dev`
(`http://<INGRESS_IP>:9221` in managed mode, `http://127.0.0.1:9221` via
`bin/port-forward.sh` in shared mode), so the same command works in both:

```bash
LOCK_URL=$(grep DATASPOKE_LOCK_URL helm-charts/.env.dev | cut -d= -f2)
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
