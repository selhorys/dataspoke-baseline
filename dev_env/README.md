# DataSpoke Development Environment

A fully scripted Kubernetes-based environment for developing and testing DataSpoke. Three namespaces are provisioned: `datahub-01` (DataHub), `dataspoke-01` (infrastructure), and `dataspoke-dummy-data-01` (example data sources).

The API runs **in-cluster** alongside Airflow so that workflow callbacks work via cluster DNS. Developers access the API via nginx-ingress (`http://app.<INGRESS_IP>.nip.io/api/v1/`). Frontend runs on the host. See [spec/TESTING.md §Testing Modes](../spec/TESTING.md#testing-modes).

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- A Kubernetes cluster with **8+ CPUs / 24 GB RAM**

## Quick Start

> Using Claude Code? Just run `/dev-env install`.

### 1. Configure

```bash
cp .env.example .env
# Edit .env — set DATASPOKE_DEV_KUBE_CLUSTER to your context
# Set DATASPOKE_DEV_INGRESS_IP after install (written automatically by nginx-ingress/install.sh)
```

### 2. Install nginx-ingress controller (first time only)

```bash
./nginx-ingress/install.sh   # Installs ingress-nginx, waits for external IP, writes IP to .env
```

### 3. Install all other components

```bash
./install.sh    # ~5-10 min first run
```

### 4. Verify

```bash
./health-check.sh    # Checks all services via nginx-ingress endpoints
```

### Ingress Endpoints

All HTTP services are accessed via virtual-host routing on the nginx-ingress LoadBalancer IP (`DATASPOKE_DEV_INGRESS_IP`). TCP services (databases, brokers) are exposed on dedicated ports.

| Service | Address | Credentials |
|---------|---------|-------------|
| DataHub UI | `http://datahub.<INGRESS_IP>.nip.io/` | `datahub` / `datahub` |
| DataHub GMS | `http://datahub.<INGRESS_IP>.nip.io/gms/` | -- |
| DataSpoke API | `http://app.<INGRESS_IP>.nip.io/api/v1/` | per `.env` JWT |
| Airflow UI | `http://airflow.<INGRESS_IP>.nip.io/` | `admin` / `admin` (see `.env`) |
| DataSpoke PostgreSQL | `<INGRESS_IP>:9201` | per `.env` |
| Redis | `<INGRESS_IP>:9202` | per `.env` |
| DataHub Kafka | `<INGRESS_IP>:9005` | -- |
| Example PostgreSQL | `<INGRESS_IP>:9102` | `postgres` / `ExampleDev2024!` |
| Example Kafka | `<INGRESS_IP>:9104` | -- |
| Lock API | `<INGRESS_IP>:9221` | -- |

Replace `<INGRESS_IP>` with the value of `DATASPOKE_DEV_INGRESS_IP` from `dev_env/.env`. The `nip.io` suffix provides automatic wildcard DNS resolution — no `/etc/hosts` entries needed.

### 5. Deploy DataSpoke API (in-cluster)

```bash
./dataspoke-test-mode.sh              # Build image, deploy via Helm, wait for rollout
./dataspoke-test-mode.sh --skip-build # Deploy without rebuilding the image
./dataspoke-test-mode.sh --stop       # Scale down the API deployment
```

The API is accessible at `http://app.<INGRESS_IP>.nip.io/api/v1/` via nginx-ingress. For optional host-mode development (no Airflow callbacks): `uv run -m src.cli` from the repo root.

### 6. Lock service (multi-tester coordination)

Use the advisory lock before destructive operations (data resets, migrations, ingestion tests):

```bash
INGRESS_IP=$(grep DATASPOKE_DEV_INGRESS_IP dev_env/.env | cut -d= -f2)
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

Lock state is in-memory (resets on pod restart). The lock is advisory -- it does not block infra access directly.

### 7. API-wired integration tests (test mode)

Test mode (`DATASPOKE_TEST_MODE=true`) stubs LLM, cache, and notification while keeping DataHub and PostgreSQL real. See [spec/TESTING.md](../spec/TESTING.md) for the three-group test execution sequence.

```bash
./dataspoke-test-mode.sh                                          # Build + deploy
DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/
./dataspoke-test-mode.sh --stop
```

Flags: `--skip-build`, `--health-check`, `--stop`.

### 8. Populate dummy data

```bash
uv run python -m tests.integration.util --reset-all   # Idempotent: PG + Kafka + DataHub
```

Seeds 11 schemas, 17 tables (~600 rows), 3 Kafka topics (~45 messages), and 20 DataHub dataset entities with Imazon use-case data. See `spec/feature/DEV_ENV.md §Dummy Data` for details.

> After a fresh reinstall, trigger the `embedding-sync` DAG with `mode=full` to rebuild the `dataspoke.dataset_embeddings` pgvector table in PostgreSQL.

## Selective Reinstall

Reinstall a single component by running its `uninstall.sh` followed by `install.sh` (both are idempotent and tear down PVCs + Helm release within their scope):

```bash
# Example: reset dataspoke-infra (PostgreSQL, Redis, Airflow, API)
bash dataspoke-infra/uninstall.sh && bash dataspoke-infra/install.sh
```

## Uninstall

```bash
./uninstall.sh    # Prompts before destructive operations (includes nginx-ingress teardown)
```

## Reference

### nginx-ingress controller

The nginx-ingress controller lives in the `ingress-nginx` namespace and is installed/uninstalled independently of the application namespaces:

```bash
./nginx-ingress/install.sh    # Install controller, wait for IP, write to .env
./nginx-ingress/uninstall.sh  # Remove controller (run after ./uninstall.sh)
```

The controller serves:
- **HTTP virtual hosts** on port 80 (and 443 for TLS) for DataHub, DataSpoke API, DataSpoke UI, and Airflow
- **TCP passthrough** on dedicated ports (9201-9202, 9005, 9102, 9104, 9221) for databases, brokers, and the lock service

### Namespace architecture

| Namespace | Purpose | Managed by |
|-----------|---------|------------|
| `ingress-nginx` | nginx-ingress controller | `nginx-ingress/install.sh` |
| `datahub-01` | DataHub + backing services | `datahub/install.sh` |
| `dataspoke-01` | DataSpoke infrastructure + lock service | `dataspoke-infra/install.sh`, `dataspoke-lock/install.sh` |
| `dataspoke-dummy-data-01` | Example PostgreSQL + Kafka | `dataspoke-example/install.sh` |

### Environment variables

Two-tier naming convention in `.env`:

| Prefix | Scope | Example |
|--------|-------|---------|
| `DATASPOKE_DEV_*` | Dev scripts only | `DATASPOKE_DEV_KUBE_CLUSTER`, `DATASPOKE_DEV_INGRESS_IP` |
| `DATASPOKE_*` (no `DEV`) | App runtime | `DATASPOKE_POSTGRES_HOST` |

### Resource budget

Sum of memory *limits* ≈ 25 GiB (above 24 GB cluster capacity); sum of *requests* ≈ 13 GiB. Pods rarely hit limits simultaneously. See [`spec/feature/DEV_ENV.md §Resource Budget`](../spec/feature/DEV_ENV.md#resource-budget) for per-component breakdown and rationale.

## Troubleshooting

See [spec/feature/DEV_ENV.md §Troubleshooting](../spec/feature/DEV_ENV.md#troubleshooting).
