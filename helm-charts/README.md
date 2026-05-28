# DataSpoke Helm Charts

Single deployment subsystem for DataSpoke — both production and development.
Scripts live in `helm-charts/bin/`, chart values in `helm-charts/dataspoke/`, and
dev-only peripheral manifests in `helm-charts/peripherals/`.

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM** (dev) or operator-specified sizing (prod)

## Quick Start

### Configure

```bash
cp helm-charts/.env.example helm-charts/.env
# Edit helm-charts/.env — set DATASPOKE_KUBE_CLUSTER, DATASPOKE_KUBE_IMAGE_REGISTRY, etc.
```

### Dev profile (full install)

```bash
./helm-charts/bin/install.sh --profile dev
```

### Health check

```bash
./helm-charts/bin/health-check.sh
```

### Uninstall

```bash
./helm-charts/bin/uninstall.sh --profile dev
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
| DataSpoke UI (prod / `--components frontend`) | `http://app.<INGRESS_IP>.nip.io/` | login via DataSpoke auth |
| DataSpoke API | `http://app.<INGRESS_IP>.nip.io/api/v1/` | per `.env` JWT |
| Airflow UI | `http://airflow.<INGRESS_IP>.nip.io/` | `admin` / `admin` (see `.env`) |
| Langfuse UI | `http://langfuse.<INGRESS_IP>.nip.io/` | `DATASPOKE_DEV_LANGFUSE_INIT_USER_{EMAIL,PASSWORD}` in `helm-charts/.env` (auto-generated on first install) |
| DataSpoke PostgreSQL | `<INGRESS_IP>:9201` | per `.env` |
| Redis | `<INGRESS_IP>:9202` | per `.env` |
| DataHub Kafka | `<INGRESS_IP>:9005` | -- |
| Example PostgreSQL | `<INGRESS_IP>:9102` | `postgres` / `ExampleDev2024!` |
| Example Kafka | `<INGRESS_IP>:9104` | -- |
| Lock API | `<INGRESS_IP>:9221` | -- |

Dev frontend runs on the host (`pnpm dev` in `src/frontend/`) — the in-cluster
frontend pod is disabled by default. To deploy the containerised frontend in dev:
`./helm-charts/bin/install.sh --profile dev --components frontend`.
Runtime config vars (`DATASPOKE_API_BASE_URL`, `DATASPOKE_DATAHUB_URL`) are
injected via ConfigMap; values come from `frontend.config.*` in `values.yaml`.

Replace `<INGRESS_IP>` with the value of `DATASPOKE_KUBE_INGRESS_IP` from
`helm-charts/.env`. The `nip.io` suffix provides automatic wildcard DNS
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

```bash
INGRESS_IP=$(grep DATASPOKE_KUBE_INGRESS_IP helm-charts/.env | cut -d= -f2)
curl -s -X POST http://${INGRESS_IP}:9221/lock/acquire \
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
