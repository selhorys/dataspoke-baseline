# DataSpoke Development Environment

A fully scripted Kubernetes-based environment for developing and testing DataSpoke. Three namespaces are provisioned: `datahub-01` (DataHub), `dataspoke-01` (infrastructure), and `dataspoke-dummy-data-01` (example data sources).

By default, the cluster hosts only **infrastructure dependencies**. DataSpoke application services run on your host machine, connecting to port-forwarded infrastructure (host mode). For in-cluster testing, see [spec/TESTING.md §Testing Modes](../spec/TESTING.md#testing-modes).

## Prerequisites

- `kubectl` installed and configured
- `helm` v3 installed
- A Kubernetes cluster with **8+ CPUs / 16 GB RAM**

## Quick Start

> Using Claude Code? Just run `/dev-env install`.

### 1. Configure

```bash
cp .env.example .env
# Edit .env — set DATASPOKE_DEV_KUBE_CLUSTER to your context (e.g., minikube, docker-desktop)
```

### 2. Install

```bash
./install.sh    # ~5-10 min first run
```

### 3. Port-forward and verify

```bash
./datahub-port-forward.sh       # DataHub UI (9002) + GMS (9004) + Kafka (9005)
./dataspoke-port-forward.sh     # PostgreSQL (9201), Redis (9202), Qdrant (9203-4), Kestra (9205)
./dummy-data-port-forward.sh    # Example PostgreSQL (9102), Kafka (9104)
./lock-port-forward.sh          # Advisory lock (9221)
./health-check.sh               # Verify all services respond
```

All port-forward scripts support `--stop` to terminate.

| Service | Address | Credentials |
|---------|---------|-------------|
| DataHub UI | http://localhost:9002 | `datahub` / `datahub` |
| DataHub GMS | http://localhost:9004 | -- |
| DataSpoke PostgreSQL | localhost:9201 | per `.env` |
| Redis | localhost:9202 | per `.env` |
| Qdrant | localhost:9203 (HTTP), :9204 (gRPC) | -- |
| Kestra | http://localhost:9205 | -- |
| Example PostgreSQL | localhost:9102 | `postgres` / `ExampleDev2024!` |
| Example Kafka | localhost:9104 | -- |
| Lock API | http://localhost:9221 | -- |

### 4. Run DataSpoke (host mode)

```bash
uv sync              # Install Python dependencies (from repo root)
uv run -m src.cli    # Start API + auto-migrate
uv run -m src.cli --help   # All options
```

### 5. Lock service (multi-tester coordination)

Use the advisory lock before destructive operations (data resets, migrations, ingestion tests):

```bash
curl -s -X POST http://localhost:9221/lock/acquire \
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

### 6. API-wired integration tests (test mode)

Test mode (`DATASPOKE_TEST_MODE=true`) stubs LLM, Qdrant, cache, and notification while keeping DataHub and PostgreSQL real. See [spec/TESTING.md](../spec/TESTING.md) for the three-group test execution sequence.

```bash
./dataspoke-test-mode.sh --skip-migrate --no-reload &
until curl -s http://localhost:8000/health > /dev/null 2>&1; do sleep 2; done
DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/
./dataspoke-test-mode.sh --stop
```

Flags: `--skip-migrate`, `--no-reload`, `--port <N>`, `--health-check`, `--stop`.

### 7. Populate dummy data

```bash
uv run python -m tests.integration.util --reset-all   # Idempotent: PG + Kafka + DataHub
```

Seeds 11 schemas, 17 tables (~600 rows), 3 Kafka topics (~45 messages), and 20 DataHub dataset entities with Imazon use-case data. See `spec/feature/DEV_ENV.md §Dummy Data` for details.

## Uninstall

```bash
./uninstall.sh    # Prompts before destructive operations
```

## Reference

### Namespace architecture

| Namespace | Purpose | Managed by |
|-----------|---------|------------|
| `datahub-01` | DataHub + backing services | `datahub/install.sh` |
| `dataspoke-01` | DataSpoke infrastructure + lock service | `dataspoke-infra/install.sh`, `dataspoke-lock/install.sh` |
| `dataspoke-dummy-data-01` | Example PostgreSQL + Kafka | `dataspoke-example/install.sh` |

### Environment variables

Two-tier naming convention in `.env`:

| Prefix | Scope | Example |
|--------|-------|---------|
| `DATASPOKE_DEV_*` | Dev scripts only | `DATASPOKE_DEV_KUBE_CLUSTER` |
| `DATASPOKE_*` (no `DEV`) | App runtime | `DATASPOKE_POSTGRES_HOST` |

### Resource budget

~12.3 GiB total memory limits on 8+ CPU / 16 GB cluster (~77% utilization). See [`spec/feature/DEV_ENV.md §Resource Budget`](../spec/feature/DEV_ENV.md#resource-budget) for per-component breakdown and rationale.

## Troubleshooting

See [spec/feature/DEV_ENV.md §Troubleshooting](../spec/feature/DEV_ENV.md#troubleshooting).
