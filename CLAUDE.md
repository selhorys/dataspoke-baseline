# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

DataSpoke is a sidecar extension to DataHub that provides user-group-specific features for Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG). This repo contains architecture specs, dev environment setup, and application source code. Read `spec/ARCHITECTURE.md` for full system design; read `spec/AI_SCAFFOLD.md` for scaffold details.

## Local Dev Environment

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
- **Reference when implementing**: `spec/DATAHUB_INTEGRATION.md` for DataHub interactions; `spec/feature/API.md` for routes, auth, middleware, error codes

## Spec Hierarchy

Specs must not contradict each other — propagate changes up and down. Priority order:

| Priority | Documents | Role |
|----------|-----------|------|
| 1 | `MANIFESTO_en/kr.md` | Product identity. Never modify unless explicitly requested. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Binding conventions. |
| 3 | `ARCHITECTURE.md`, `TESTING.md`, `USE_CASE_en/kr.md` | System architecture, testing conventions, and scenarios. |
| 4 | `AI_SCAFFOLD.md` | Claude Code scaffold conventions. |
| 5 | `feature/<FEATURE>.md` | Common feature specs. |
| 6 | `feature/spoke/<FEATURE>.md` | User-group-specific feature specs. |
| 7 | `impl/YYYYMMDD_<topic>.md` | Implementation logs (propagation stops here). |

When both `_en.md` and `_kr.md` exist, read only English unless directed otherwise. Write Korean in plain style (-다/-한다).

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
For spec authoring, use `/dataspoke-plan-write` or `/plan-doc` directly.
For testing conventions (unit/integration/E2E, toolchain, dev-env lock protocol), see `spec/TESTING.md`.

## Testing prauto

Due to Claude's nested-run limit, testing `.prauto/heartbeat.sh` from inside a Claude Code session requires unsetting the `CLAUDECODE` env var:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh
```

## Claude Code Configuration

**Skills**: `kubectl`, `monitor-k8s`, `plan-doc`, `datahub-api`, `prauto-check-status`, `prauto-run-heartbeat`
_(Note: `datahub-api` requires `ref/github/datahub/` — run `/dataspoke-ref-setup-all` once if not present.)_
**Commands**: `dataspoke-dev-env-install`, `dataspoke-dev-env-uninstall`, `dataspoke-ref-setup-all`, `dataspoke-plan-write`
**Subagents**: `api-spec`, `backend`, `frontend`, `k8s-helm`
**Hook**: `auto-format.sh` — auto-formats Python (ruff) and TypeScript (prettier) after edits
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
