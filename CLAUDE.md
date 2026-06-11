# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

DataSpoke is a sidecar extension to DataHub that ships a five-feature baseline (Ingestion Control, Validation, Ontology Generation, Metadata Generation, Governance) plus a Productized Scaffold (AI Scaffold + Development Scaffold) for building custom Spokes. Both UI and API organise the baseline around these five features — one function namespace each. User-group vocabulary (data engineers / analysts / stewards) appears only in `MANIFESTO_*.md` motivation. Application source code (`src/`) will be generated using the scaffold's subagents. Read `spec/MANIFESTO_en.md`, `spec/ARCHITECTURE.md`, and `spec/AI_SCAFFOLD.md` for the full picture.

## Shell Commands

Run every command from the directory it expects (usually project root). Do not `cd` away mid-session — use relative paths instead.

## Deployment

```bash
./helm-charts/bin/install.sh --profile dev        # Full dev stack (peripherals + umbrella chart)
./helm-charts/bin/uninstall.sh --profile dev      # Tear down everything
./helm-charts/bin/install.sh --profile dev --components dataspoke-infra   # Single component reinstall
./helm-charts/bin/install.sh --profile dev --components api               # Rebuild + redeploy the API
./helm-charts/bin/install.sh --profile dev --components frontend          # Rebuild + redeploy the Next.js UI
./helm-charts/bin/install.sh --profile dev --frontend local              # Full dev stack + write src/frontend/.env.local for host `pnpm dev`
```

Settings in `helm-charts/.env`. See `helm-charts/README.md` for access details and ingress endpoints; `spec/feature/HELM_CHART.md` for the full deployment subsystem.

The API runs **in-cluster** alongside Airflow so that workflow callbacks work via cluster DNS. Developers access it via nginx-ingress (`http://api.<INGRESS_IP>.nip.io/api/v1/`). Code changes are picked up by `install.sh --profile dev --components api` (docker build + `helm upgrade` + rollout).

The **frontend** (`src/frontend/`, Next.js 15 + pnpm) is a thin reference UI. A full install's `--frontend` flag (default `none` in dev, `cluster` in prod) controls it: `none` deploys nothing; `local` (dev-only) writes `src/frontend/.env.local` so host `pnpm dev` reaches the in-cluster API; `cluster` deploys the containerised UI. `--components frontend` is the standalone rebuild+redeploy iteration path. Frontend tests run via `pnpm -C src/frontend test` (Vitest), separate from the Python `pytest` groups.

Prod uses the same umbrella chart with `values.yaml` plus an operator-supplied overlay: `./helm-charts/bin/install.sh --profile prod --values <overlay.yaml>`.

## Key Design Decisions

- **DataHub-backed SSOT**: DataHub stores metadata; DataSpoke extends without modifying core
- **API-first**: FastAPI implementation in `src/api/` is the SSOT for the API contract; all APIs follow `spec/API_DESIGN_PRINCIPLE_en.md`
- **Two-axis API routing**: per-dataset cross-feature routes at `/api/v1/spoke/common/data/{dataset_urn}/…`; cross-dataset list views and global features under `/api/v1/spoke/{ingestion,validation,ontogen,metagen,governance}/…`; `/api/v1/hub/…` for DataHub pass-through
- **Airflow 3.1.8** for workflow orchestration (fixed schedule tiers + on-demand HTTP triggers, LocalExecutor); **PostgreSQL 17** (with `pgvector` for vector search and Apache `age` installed as reserved graph infrastructure) for operational DB
- **Self-hosted Langfuse** for LLM observability — sibling subsystem in its own `langfuse-01` namespace; connection (host + keys) stored in the DB `peripheral_config` table via `/api/v1/admin/peripherals/langfuse` (absence disables tracing). See `spec/feature/BACKEND_LLM.md §Observability`.
- **Headless / API-first**: backend's primary task is to support `spec/API.md`; frontend is a thin reference UI that consumes API routes verbatim (no invented endpoints); per `spec/feature/FRONTEND_BASIC.md` no streaming surface exists in the baseline — clients poll `event/...` and `attr/.../result`
- **No DataHub CLI**: The `datahub` CLI requires Python ≤ 3.11 and is incompatible with the project's Python 3.13 runtime. Use Python scripts with the `acryl-datahub` SDK instead.
- **DataHub debugging protocol**: For any DataHub integration or infrastructure issue, consult `ref/github/datahub/` source code and use the `/datahub-api` skill before guessing configs or iterating through Helm upgrades.
- **Reference when implementing**: `spec/DATAHUB_INTEGRATION.md` for DataHub interactions; `spec/API.md` for routes, auth, middleware, error codes; `spec/feature/BACKEND.md` for backend services, workflows; `spec/feature/BACKEND_LLM.md` for LLM inference loop, per-service validators, adversarial debate framework, and test-mode toggles; `spec/feature/BACKEND_SCHEMA.md` for DB schema (relational + pgvector tables); `spec/feature/FRONTEND_*.md` for UI layout, per-function pages, shared components

## Spec Convention

Specs must not contradict each other — propagate changes up and down. Priority order:

| Priority | Documents | Role |
|----------|-----------|------|
| 1 | `MANIFESTO_en/kr.md`, `API.md`, `USE_CASE_en/kr.md` | Golden product identity, API contract, and scenario set. Never modify unless explicitly requested; everything else syncs to these. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Binding conventions. |
| 3 | `ARCHITECTURE.md`, `TESTING.md` | System architecture and testing conventions. |
| 4 | `AI_SCAFFOLD.md`, `AI_PRAUTO.md` | Claude Code scaffold conventions; autonomous PR worker. |
| 5 | `feature/<FEATURE>.md` | Common feature specs and per-function FRONTEND specs (`FRONTEND_{BASIC,GOVERNANCE,INGESTION,VALIDATION,ONTOGEN,METAGEN}.md`). |

When both `_en.md` and `_kr.md` exist, read only English unless directed otherwise. Write Korean in plain style (-다/-한다).

In spec, focus on architecture, decisions, and constraints. From spec, remove verbatim template code, full code blocks, and script snippets that duplicate the impl files.

## Git Commit Convention

- Conventional Commits: `<type>: <subject>` (e.g. `feat:`, `fix:`, `docs:`, `refactor:`)
- **Always run `git diff` (or `git diff --staged`) and base the commit message on the actual diff output**, not on prior conversation context or memory of what was changed
- Body optional, **max 15 lines, max 100 chars per line** if included

## Implementation Workflow

The scaffold uses a **plan → approve → generate → evaluate** architecture. This separation prevents the self-praise failure mode where agents approve their own mediocre work.

**You MUST enter Plan mode before writing any implementation code** unless the change meets **all** of these skip-plan criteria:
- Touches < 3 files and adds/modifies < 60 lines of logic
- Does not introduce a new API endpoint, DB table/column, pgvector collection, or Airflow DAG
- Does not require coordination across layers (backend + frontend, backend + workflow, etc.)
- The user explicitly says "just do it" / "quick fix" / "no need to plan"

When in doubt, plan. Never self-classify a task as "trivial" to skip planning.

End-to-end steps:

1. Read the relevant spec in `spec/feature/`
2. **Plan (built-in Plan mode)** — produce implementation plan with files, contracts, and acceptance criteria. The plan MUST specify which generator agents (`backend`, `airflow-dag`, `frontend`, `test`, `k8s-helm`) to launch and in what order. Steps 4–8 are driven by direct Agent calls by default; the plan may opt into the `feature-impl` workflow for large multi-stage features (§Orchestration) — plan approval doubles as that opt-in. See `spec/AI_SCAFFOLD.md` §Plan quality checklist.
3. **Human approves the plan** — do NOT proceed to code generation without explicit approval
4. `backend` agent → `reviewer` agent → [fix pass if REVISE, max 1 iteration]
5. `airflow-dag` agent → `reviewer` agent → [fix pass if REVISE, max 1 iteration]
   (steps 4 and 5 may run concurrently when the DAG work does not depend on new backend API contracts)
6. `test` agent → `test-reviewer` agent → [fix pass if REVISE, max 1 iteration]
7. `frontend` agent → `reviewer` agent → [fix pass if REVISE, max 1 iteration]
8. `k8s-helm` agent — containerize and deploy (when ready, no review loop)

When a generator's diff touches paths listed in `.claude/agents/security-reviewer.md`, also run `security-reviewer` in parallel with `reviewer`; merge their findings before deciding APPROVE / REVISE / ESCALATE.

**Orchestration**: drive steps 4–8 with direct `Agent` calls by default — spawn each generator, then its reviewer(s), following the per-stage loop in steps 4–8 and the delegation rules below. The non-negotiable rule is generator ≠ reviewer: every reviewed stage gets a separate `reviewer` (or `test-reviewer`) before later stages build on it. An ESCALATE (or a REVISE persisting after one fix pass) halts the run — relay the findings to the user and wait. Reach for the checked-in `feature-impl` dynamic workflow (`.claude/workflows/feature-impl.js`, `args = {plan, stages, security}` — `stages` in plan order with concurrent stages grouped in inner arrays, `security` naming sensitive-path stages) only when a feature spans several generator stages at once and its deterministic fan-out / parallelism earns the extra tokens; the script mechanizes this same loop. Plan approval is standing authorization to invoke the Workflow tool when the plan opts in; the per-run launch prompt remains the user's control point.

Delegate implementation to the appropriate generator agent rather than writing code directly in the main conversation. Each generator runs in a confined context — it sees only the approved plan, the relevant spec, and the files in its scope. The reviewer receives the plan + generator's completion report + changed files. If the reviewer's verdict is REVISE, the generator is re-invoked with the findings for a fix pass. If issues persist after one fix pass, they are escalated to the user.

For spec authoring, use `/spec-write` directly.
For testing conventions (unit/integration/api-wired integration/E2E, toolchain, dev-env lock protocol), see `spec/TESTING.md`.

## Integration Test Protocol

Follow `spec/TESTING.md §Integration Testing` for the full 7-step workflow, pre-flight + reinstall table, lock protocol, data reset, Imazon test-data rule, assertion rules, and manual API testing. Key reminders:

- Run `./helm-charts/bin/health-check.sh` before any integration test run; reinstall any failing subsystem per `spec/TESTING.md §Prerequisites` before proceeding.
- Run tests in three **separate** groups (unit → spot integration → api-wired integration). Mixing causes Airflow resource contention.
- Spot/api-wired tests need `helm-charts/.env` exported into the shell. Stub-mode toggles are DB-backed in `/admin/conf` (per memory `project_runtime_config_admin_conf`), not env-driven. Canonical command: `set -a && source helm-charts/.env && set +a && uv run pytest tests/integration/{spot,api_wired}/` (the `set -a` is required because `helm-charts/.env` has no `export` prefixes).
- Never truncate integration test output (no `| tail`, `| head`, or piped filters) — always show complete pytest output.

## Testing prauto

Due to Claude's nested-run limit, testing `.prauto/heartbeat.sh` from inside a Claude Code session requires unsetting the `CLAUDECODE` env var:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh
```

## Claude Code Configuration

**Skills**: `k8s-work`, `spec-write`, `datahub-api`, `prauto-check-status`, `prauto-run-heartbeat`, `k8s-deploy`, `ref-setup`, `spec-sync-with-impl`, `spec-harmonize`, `spec-reduce`, `spec-to-bulk-issue`, `test-api-wired-manual`
_(Note: `datahub-api` requires `ref/github/datahub/` — run `/ref-setup` once if not present.)_
**Subagents**: `reviewer` (evaluator, opus), `test-reviewer` (evaluator, opus), `security-reviewer` (evaluator, opus), `backend`, `airflow-dag`, `test`, `frontend`, `k8s-helm`. Evaluators keep persistent cross-session memory in `.claude/agent-memory/<name>/` (checked in).
**Workflows**: `.claude/workflows/feature-impl.js` — optional generate→evaluate orchestration for §Implementation Workflow steps 4–8, used for large multi-stage features; the default driver is direct Agent calls (also runnable as `/feature-impl`).
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
**Hooks**: `.claude/hooks/` — integration-test preflight (blocking), plan-gate reminder, permission-hygiene warning, commit confirmation. Wired via `.claude/settings.json`; tool-event hooks are gated by settings-level `if` filters with in-script guards as backup. Generator agents additionally run per-agent hooks (ruff on edited Python files; frontend typecheck on Stop), wired in their frontmatter.
**Statusline**: `.claude/statusline.sh` — model · effort · cwd · git-branch · 5-hour block reset countdown. Reset segment requires `ccusage` on `$PATH` (`npm i -g ccusage` on node ≥ 18, or `brew install bun && bun add -g ccusage`); omitted silently if unavailable.
