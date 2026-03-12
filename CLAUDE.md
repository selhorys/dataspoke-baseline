# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

DataSpoke is a sidecar extension to DataHub that provides user-group-specific features for Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG). This repo contains architecture specs, dev environment setup, and an AI coding scaffold. Application source code (`src/`) will be generated using the scaffold's subagents. Read `spec/ARCHITECTURE.md` for full system design; read `spec/AI_SCAFFOLD.md` for scaffold details.

## Dev Environment

```bash
cd dev_env && ./install.sh    # Install infrastructure (DataHub, PostgreSQL, Redis, Qdrant, Temporal)
cd dev_env && ./uninstall.sh  # Tear down everything
```

Settings in `dev_env/.env`. See `dev_env/README.md` for access details and port-forwarding.

## Key Design Decisions

- **DataHub-backed SSOT**: DataHub stores metadata; DataSpoke extends without modifying core
- **API-first**: OpenAPI specs in `api/` as standalone artifacts; all APIs follow `spec/API_DESIGN_PRINCIPLE_en.md`
- **Three-tier API routing**: `/api/v1/spoke/common/…`, `/api/v1/spoke/[de|da|dg]/…`, `/api/v1/hub/…`
- **Temporal** for orchestration, **Qdrant** for vector search, **PostgreSQL** for operational DB
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

For end-to-end feature implementation, use subagents in this order:

1. Read the relevant spec in `spec/feature/` or `spec/feature/spoke/`
2. `api-spec` agent — write OpenAPI spec in `api/`
3. `backend` agent — implement API routes + services in `src/`
4. `frontend` agent — build UI in `src/frontend/`
5. `k8s-helm` agent — containerize and deploy (when ready)

Each agent reads the spec and the output of previous agents as context.
For spec authoring, use `/plan-doc` directly.
For testing conventions (unit/integration/E2E, toolchain, dev-env lock protocol), see `spec/TESTING.md`.

## Integration Test Protocol

Follow `spec/TESTING.md §Integration Testing` (7-step workflow). Key rules:

**Infrastructure**: host mode against port-forwarded dev-env infra. Ensure port-forwards are active before running tests.

**Lock protocol**: acquire the dev-env advisory lock before any state-mutating operation.

**Data reset**: `conftest.py` automatically resets dummy data via Python utilities in `tests/integration/util/` before and after test runs. For manual reset: `uv run python -m tests.integration.util --reset-all`

**Dummy-data fixtures**: SQL seed files, Kafka JSONL messages, and DataHub ingestion logic live in `tests/integration/util/`.

**Test data**: all integration/E2E scenarios use **Imazon** as the canonical company context — do not invent alternative test companies.

**Assertion rules**:
- Never hardcode row counts — query actual counts within the test
- Never hardcode surrogate IDs — look up by stable natural key (ISBN, URN, email)
- Never assert on wall-clock timestamps — assert on relative ordering or freshness windows

## Testing prauto

Due to Claude's nested-run limit, testing `.prauto/heartbeat.sh` from inside a Claude Code session requires unsetting the `CLAUDECODE` env var:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh
```

## Claude Code Configuration

**Skills**: `k8s-work`, `plan-doc`, `datahub-api`, `prauto-check-status`, `prauto-run-heartbeat`, `dev-env`, `ref-setup`, `sync-spec-from-impl`, `sync-specs`, `spec-to-bulk-issue`
_(Note: `datahub-api` requires `ref/github/datahub/` — run `/ref-setup` once if not present.)_
**Subagents**: `api-spec`, `backend`, `frontend`, `k8s-helm`
**Hook**: `auto-format.sh` — auto-formats Python (`uv run ruff`, falls back to bare `ruff`) and TypeScript (`prettier`) after edits
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
