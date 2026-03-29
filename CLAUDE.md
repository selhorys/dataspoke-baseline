# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

DataSpoke is a sidecar extension to DataHub that provides user-group-specific features for Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG). This repo contains architecture specs, dev environment setup, and an AI coding scaffold. Application source code (`src/`) will be generated using the scaffold's subagents. Read `spec/ARCHITECTURE.md` for full system design; read `spec/AI_SCAFFOLD.md` for scaffold details.

## Shell Commands

Run every command from the directory it expects (usually project root). Do not `cd` away mid-session — use relative paths instead.

## Dev Environment

```bash
cd dev_env && ./install.sh    # Install infrastructure (DataHub, PostgreSQL, Redis, Qdrant, Kestra)
cd dev_env && ./uninstall.sh  # Tear down everything
```

Settings in `dev_env/.env`. See `dev_env/README.md` for access details and port-forwarding.

Quick-start (host mode): `uv run -m src.cli` starts API + runs migrations. Workflow orchestration is handled by Kestra (running in K8s). See `uv run -m src.cli --help` for options.

The dev environment uses the same umbrella Helm chart as production (`helm-charts/dataspoke/`) with a dev overlay (`values-dev.yaml`). Two testing modes: **host** (default — app on host, infra in cluster) and **in-cluster** (on-demand — all components in K8s via `--set` flags). See `spec/TESTING.md §Testing Modes`.

## Key Design Decisions

- **DataHub-backed SSOT**: DataHub stores metadata; DataSpoke extends without modifying core
- **API-first**: FastAPI implementation in `src/api/` is the SSOT for the API contract; all APIs follow `spec/API_DESIGN_PRINCIPLE_en.md`
- **Three-tier API routing**: `/api/v1/spoke/common/…`, `/api/v1/spoke/[de|da|dg]/…`, `/api/v1/hub/…`
- **Kestra** for workflow orchestration (HTTP-triggered, YAML flows), **Qdrant** for vector search, **PostgreSQL** for operational DB
- **No DataHub CLI**: The `datahub` CLI requires Python ≤ 3.11 and is incompatible with the project's Python 3.13 runtime. Use Python scripts with the `acryl-datahub` SDK instead.
- **Reference when implementing**: `spec/DATAHUB_INTEGRATION.md` for DataHub interactions; `spec/feature/API.md` for routes, auth, middleware, error codes; `spec/feature/BACKEND.md` for backend services, workflows; `spec/feature/BACKEND_SCHEMA.md` for DB schema, Qdrant collections; `spec/feature/FRONTEND_*.md` for UI layout, workspace pages, shared components

## Spec Convention

Specs must not contradict each other — propagate changes up and down. Priority order:

| Priority | Documents | Role |
|----------|-----------|------|
| 1 | `MANIFESTO_en/kr.md` | Product identity. Never modify unless explicitly requested. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Binding conventions. |
| 3 | `ARCHITECTURE.md`, `TESTING.md`, `USE_CASE_en/kr.md` | System architecture, testing conventions, and scenarios. |
| 4 | `AI_SCAFFOLD.md`, `AI_PRAUTO.md` | Claude Code scaffold conventions; autonomous PR worker. |
| 5 | `feature/<FEATURE>.md` | Common feature specs. |
| 6 | `feature/spoke/<FEATURE>.md` | User-group-specific feature specs. |

When both `_en.md` and `_kr.md` exist, read only English unless directed otherwise. Write Korean in plain style (-다/-한다).

In spec, focus on architecture, decisions, and constraints. From spec, remove verbatim template code, full code blocks, and script snippets that duplicate the impl files.

## Git Commit Convention

- Conventional Commits: `<type>: <subject>` (e.g. `feat:`, `fix:`, `docs:`, `refactor:`)
- **Always run `git diff` (or `git diff --staged`) and base the commit message on the actual diff output**, not on prior conversation context or memory of what was changed
- Body optional, **max 5 lines** if included

## Implementation Workflow

The scaffold uses a **plan → approve → generate → evaluate** architecture. Planning uses Claude's built-in Plan mode interactively; generators write code and self-test; an independent reviewer evaluates the output against the spec and plan. This separation prevents the self-praise failure mode where agents approve their own mediocre work.

For end-to-end feature implementation:

1. Read the relevant spec in `spec/feature/` or `spec/feature/spoke/`
2. Plan (built-in Plan mode) — produce implementation plan with files, contracts, and acceptance criteria. See `spec/AI_SCAFFOLD.md` §Plan quality checklist for what a good plan covers
3. Human reviews and approves the plan (or iterates)
4. `backend` agent → `reviewer` agent → [fix pass if REVISE verdict, max 1 iteration]
5. `workflow` agent → `reviewer` agent → [fix pass if REVISE verdict, max 1 iteration]
   (steps 4 and 5 may run concurrently when workflow does not depend on new backend API contracts)
6. `test` agent — write and run tests (can also verify specific reviewer findings)
7. `frontend` agent → `reviewer` agent → [fix pass if REVISE verdict, max 1 iteration]
8. `k8s-helm` agent — containerize and deploy (when ready, no review loop)

For non-trivial implementation, delegate to the appropriate generator agent rather than writing code directly in the main conversation. Each generator runs in a confined context — it sees only the approved plan, the relevant spec, and the files in its scope. This keeps the main conversation clean for orchestration. The reviewer receives the plan + generator's completion report + changed files. If the reviewer's verdict is REVISE, the generator is re-invoked with the findings for a fix pass. If issues persist after one fix pass, they are escalated to the user.

For spec authoring, use `/plan-doc` directly.
For testing conventions (unit/integration/api-wired integration/E2E, toolchain, dev-env lock protocol), see `spec/TESTING.md`.

## Integration Test Protocol

Follow `spec/TESTING.md §Integration Testing` (7-step workflow). Key rules:

**Pre-flight**: Run `./dev_env/health-check.sh` before integration tests. It verifies all port-forwarded peripherals are reachable AND responding (not just that port-forward processes exist). Do not proceed if any check fails — reinstall the failing component's subsystem:

| Failing service | Reinstall |
|---|---|
| dataspoke-postgresql, redis, qdrant, kestra | `cd dev_env && bash dataspoke-infra/uninstall.sh && bash dataspoke-infra/install.sh` |
| datahub-gms, datahub-kafka | `cd dev_env && bash datahub/uninstall.sh && bash datahub/install.sh` |
| example-postgres, example-kafka | `cd dev_env && bash dataspoke-example/uninstall.sh && bash dataspoke-example/install.sh` |
| lock-service | `cd dev_env && bash dataspoke-lock/uninstall.sh && bash dataspoke-lock/install.sh` |

**Lock protocol**: acquire the dev-env advisory lock before any state-mutating operation.

**Data reset**: `conftest.py` automatically resets dummy data via Python utilities in `tests/integration/util/` before and after test runs. For manual reset: `uv run python -m tests.integration.util --reset-all`

**Dummy-data fixtures**: SQL seed files, Kafka JSONL messages, and DataHub ingestion logic live in `tests/integration/util/`.

**Test data**: all integration/E2E scenarios use **Imazon** as the canonical company context — do not invent alternative test companies.

**Assertion rules**:
- Never hardcode row counts — query actual counts within the test
- Never hardcode surrogate IDs — look up by stable natural key (ISBN, URN, email)
- Never assert on wall-clock timestamps — assert on relative ordering or freshness windows

**Test execution groups**: Run tests in three separate groups, do not mix:
1. `uv run pytest tests/unit/`
2. `uv run pytest tests/integration/ --ignore=tests/integration/api_wired/`
3. Start `./dev_env/dataspoke-test-mode.sh --skip-migrate --no-reload &`, wait for health, run `DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/`, then `./dev_env/dataspoke-test-mode.sh --stop`.

Mixing groups causes Kestra overload. The `require_server` fixture verifies `DATASPOKE_TEST_MODE` is set **in the pytest process** (not just the server), server health, and `ingestion-config-sync` flow registration before api-wired tests run. The env var must be passed to the `uv run pytest` command because `dataspoke-test-mode.sh` only exports it for the server subprocess.

**Manual API testing**: See `spec/TESTING.md §Manual REST API Testing`. Start test-mode server, get a token via `POST /api/v1/auth/token`, then `curl` endpoints. Refer to spot tests in `tests/integration/api_wired/spot/` for valid URNs and payloads.

**Output rules**:
- Never truncate integration test output (no `| tail`, `| head`, or piping through filters) — always show the complete pytest output

## Testing prauto

Due to Claude's nested-run limit, testing `.prauto/heartbeat.sh` from inside a Claude Code session requires unsetting the `CLAUDECODE` env var:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh
```

## Claude Code Configuration

**Skills**: `k8s-work`, `plan-doc`, `datahub-api`, `kestra-api`, `prauto-check-status`, `prauto-run-heartbeat`, `dev-env`, `ref-setup`, `spec-sync-from-impl`, `spec-harmonize`, `spec-reduce`, `spec-to-bulk-issue`
_(Note: `datahub-api` requires `ref/github/datahub/`, `kestra-api` requires `ref/github/kestra/` — run `/ref-setup` once if not present.)_
**Subagents**: `reviewer` (evaluator, opus), `backend`, `workflow`, `test`, `frontend`, `k8s-helm`
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
