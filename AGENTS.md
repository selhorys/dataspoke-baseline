# AGENTS.md

Project instructions for any coding-agent CLI working in this repository (Claude Code, Codex, or
others). This file carries everything agent-agnostic; a CLI-specific binding file
(`CLAUDE.md` for Claude Code) adds only that CLI's mechanics on top.

## Repository Purpose

DataSpoke is a sidecar extension to DataHub that ships a five-feature baseline (Ingestion Control, Validation, Ontology Generation, Metadata Generation, Governance) plus a Productized Scaffold (AI Scaffold + Development Scaffold) for building custom Spokes. Both UI and API organise the baseline around these five features — one function namespace each. User-group vocabulary (data engineers / analysts / stewards) appears only in `MANIFESTO_*.md` motivation. Application source code (`src/`) will be generated using the scaffold's roles. Read `spec/MANIFESTO_en.md`, `spec/ARCHITECTURE.md`, and `spec/AI_SCAFFOLD.md` for the full picture.

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

Settings in `helm-charts/.env.dev` (dev) / `helm-charts/.env.prod` (prod); see `helm-charts/.env.dev.example`. `DATASPOKE_KUBE_INGRESS_MODE` selects `managed` (default — install & own nginx-ingress; GKE/minikube) or `shared` (reuse a pre-existing cluster controller; AWS/EKS — TCP services then reached on 127.0.0.1 via `./helm-charts/bin/port-forward.sh`). See `helm-charts/README.md` for access details and ingress endpoints; `spec/feature/HELM_CHART.md` for the full deployment subsystem.

The API runs **in-cluster** alongside Airflow so that workflow callbacks work via cluster DNS. Developers access it via nginx-ingress (`http://api.<INGRESS_DOMAIN>/api/v1/`, where the scheme follows `DATASPOKE_KUBE_INGRESS_SCHEME` and the domain is `<IP>.nip.io` in managed mode or the operator's host in shared mode). Code changes are picked up by `install.sh --profile dev --components api` (docker build + `helm upgrade` + rollout).

The **frontend** (`src/frontend/`, Next.js 15 + pnpm) is a thin reference UI. A full install's `--frontend` flag (default `none` in dev, `cluster` in prod) controls it: `none` deploys nothing; `local` (dev-only) writes `src/frontend/.env.local` so host `pnpm dev` reaches the in-cluster API; `cluster` deploys the containerised UI. `--components frontend` is the standalone rebuild+redeploy iteration path. Frontend tests run via `pnpm -C src/frontend test` (Vitest, mocked — also runs `typecheck` first via the `pretest` script); full-stack browser E2E lives in `tests/e2e/` (Playwright — use-case + ground groups) and runs against the `--frontend cluster` UI via `pnpm -C tests/e2e test`, separate from the Python `pytest` groups.

Prod uses the same umbrella chart with `values.yaml` plus an operator-supplied overlay: `./helm-charts/bin/install.sh --profile prod --values <overlay.yaml>`.

## Key Design Decisions

- **DataHub-backed SSOT**: DataHub stores metadata; DataSpoke extends without modifying core
- **API-first**: FastAPI implementation in `src/api/` is the SSOT for the API contract; all APIs follow `spec/API_DESIGN_PRINCIPLE_en.md`
- **Two-axis API routing**: per-dataset cross-feature routes at `/api/v1/spoke/common/data/{dataset_urn}/…`; cross-dataset list views and global features under `/api/v1/spoke/{ingestion,validation,ontogen,metagen,governance}/…`
- **Airflow 3.1.8** for workflow orchestration (fixed schedule tiers + on-demand HTTP triggers, LocalExecutor); **PostgreSQL 17** (with `pgvector` for vector search and Apache `age` installed as reserved graph infrastructure) for operational DB
- **Self-hosted Langfuse** for LLM observability — dev-only peripheral in its own `langfuse-01` namespace (prod: operator-supplied); connection (host + keys) stored in the DB `peripheral_config` table via `/api/v1/admin/peripherals/langfuse` (absence disables tracing). See `spec/feature/BACKEND_LLM.md §Observability`.
- **Headless / API-first**: backend's primary task is to support `spec/API.md`; frontend is a thin reference UI that consumes API routes verbatim (no invented endpoints); per `spec/feature/FRONTEND_BASIC.md` no streaming surface exists in the baseline — clients poll `event/...` and `attr/.../result`
- **No DataHub CLI**: The `datahub` CLI requires Python ≤ 3.11 and is incompatible with the project's Python 3.13 runtime. Use Python scripts with the `acryl-datahub` SDK instead.
- **DataHub debugging protocol**: For any DataHub integration or infrastructure issue, consult `ref/github/datahub/` source code before guessing configs or iterating through Helm upgrades.
- **Reference when implementing**: `spec/DATAHUB_INTEGRATION.md` for DataHub interactions; `spec/API.md` for routes, auth, middleware, error codes; `spec/feature/BACKEND.md` for backend services, workflows; `spec/feature/BACKEND_LLM.md` for LLM inference loop, per-service validators, adversarial debate framework, and test-mode toggles; `spec/feature/BACKEND_SCHEMA.md` for DB schema (relational + pgvector tables); `spec/feature/FRONTEND_*.md` for UI layout, per-function pages, shared components

## Spec Convention

Specs must not contradict each other — propagate changes up and down. Priority order:

| Priority | Documents | Role |
|----------|-----------|------|
| 1 | `MANIFESTO_en/kr.md`, `API.md`, `USE_CASE_en/kr.md` | Golden product identity, API contract, and scenario set. Never modify unless explicitly requested; everything else syncs to these. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Binding conventions. |
| 3 | `ARCHITECTURE.md`, `TESTING.md` | System architecture and testing conventions. |
| 4 | `AI_SCAFFOLD.md`, `AI_PRAUTO.md`, `AI_PLUGIN.md` | Coding-agent scaffold conventions; autonomous PR worker; end-user plugin. |
| 5 | `feature/<FEATURE>.md` | Common feature specs and per-function FRONTEND specs (`FRONTEND_{BASIC,GOVERNANCE,INGESTION,VALIDATION,ONTOGEN,METAGEN}.md`). |

When both `_en.md` and `_kr.md` exist, read only English unless directed otherwise. Write Korean in plain style (-다/-한다).

In spec, focus on architecture, decisions, and constraints. From spec, remove verbatim template code, full code blocks, and script snippets that duplicate the impl files.

## Git Commit Convention

- Conventional Commits: `<type>: <subject>` (e.g. `feat:`, `fix:`, `docs:`, `refactor:`)
- **Always run `git diff` (or `git diff --staged`) and base the commit message on the actual diff output**, not on prior conversation context or memory of what was changed
- Body optional, **max 15 lines, max 100 chars per line** if included
- Keep `git commit` message-format conventions in mind, but there is no enforced commit-msg hook in this repo — treat this as a style rule, not a hard gate

## Implementation Workflow

The scaffold uses a **plan → approve → generate → evaluate** architecture. This separation prevents the self-praise failure mode where agents approve their own mediocre work.

**Get an approved plan before writing any implementation code** unless the change meets **all** of these skip-plan criteria:
- Touches < 3 files and adds/modifies < 60 lines of logic
- Does not introduce a new API endpoint, DB table/column, pgvector collection, or Airflow DAG
- Does not require coordination across layers (backend + frontend, backend + workflow, etc.)
- The user explicitly says "just do it" / "quick fix" / "no need to plan"

When in doubt, plan. Never self-classify a task as "trivial" to skip planning.

End-to-end steps:

1. Read the relevant spec in `spec/feature/`
2. **Plan** — produce an implementation plan with files, contracts, and acceptance criteria, and
   get it approved before any implementation code is written. Name which generator roles
   (`spec`, `backend`, `airflow-dag`, `frontend`, `test`, `k8s-helm` — defined in
   `scaffold/roles/`) the plan needs, and in what order. See `spec/AI_SCAFFOLD.md` §Plan quality
   checklist for what a good plan covers.
3. **Human approves the plan** — do NOT proceed to code generation without explicit approval
4. `spec` role → `spec-reviewer` role → [fix pass if REVISE, max 1 iteration]
   (only when the plan adds or changes specs; produces the spec the code stages read — skip if no spec change)
5. `backend` role → `reviewer` role → [fix pass if REVISE, max 1 iteration]
6. `airflow-dag` role → `reviewer` role → [fix pass if REVISE, max 1 iteration]
   (steps 5 and 6 may run concurrently when the DAG work does not depend on new backend API contracts)
7. `test` role → `test-reviewer` role → [fix pass if REVISE, max 1 iteration]
8. `frontend` role → `reviewer` role → [fix pass if REVISE, max 1 iteration]
9. `k8s-helm` role — containerize and deploy (when ready, no review loop)

When a generator's diff touches paths listed in `scaffold/roles/security-reviewer.md`, also run
`security-reviewer` in parallel with `reviewer`; merge their findings before deciding
APPROVE / REVISE / ESCALATE.

**Delegation**: every role's full definition lives in `scaffold/roles/<name>.md` — reading list,
source layout, conventions, invocation modes, and (for evaluators) the scoring rubric. How a role
gets invoked depends on the coding-agent CLI driving the session:
- **Claude Code**: use the native `Agent` tool against `.claude/agents/<name>.md` (a thin binding
  pointing at the shared role file), and its native `Workflow` tool
  (`.claude/workflows/wf-minimal.js`) to drive a multi-stage run when useful.
- **Other CLIs without a built-in subagent/workflow primitive (e.g. Codex)**: drive one role at a
  time with `scaffold/bin/run-stage.sh`, or a full stage loop with `scaffold/bin/run-workflow.sh`
  — see `scaffold/README.md`.

The non-negotiable rule regardless of CLI: generator ≠ reviewer — every reviewed stage gets a
separate evaluator role before later stages build on it. An ESCALATE (or a REVISE persisting
after one fix pass) halts the run — relay the findings to the user and wait.

For testing conventions (unit/integration/api-wired integration/E2E, toolchain, dev-env lock
protocol), see `spec/TESTING.md`. E2E (`tests/e2e/`) is Playwright with two groups — use-case
(mirrors api-wired UC stories, dual UI+backend confirmation) and ground (narrow per-page flows,
spot analogue); together they cover every `src/frontend/app/` route (tracked in
`tests/e2e/COVERAGE.md`).

## Integration Test Protocol

Follow `spec/TESTING.md §Integration Testing` for the full 7-step workflow, pre-flight + reinstall table, lock protocol, data reset, Imazon test-data rule, assertion rules, and manual API testing. Key reminders:

- Run `./helm-charts/bin/health-check.sh` before any integration test run; reinstall any failing subsystem per `spec/TESTING.md §Prerequisites` before proceeding. (`tests/integration/conftest.py`'s `require_server` autouse fixture also asserts server/stub health at session start, regardless of which agent or human invokes pytest.)
- Run tests in three **separate** groups (unit → spot integration → api-wired integration). Mixing causes Airflow resource contention.
- Spot/api-wired tests need `helm-charts/.env.dev` exported into the shell. Stub-mode toggles are DB-backed in `/admin/conf`, not env-driven. Canonical command: `set -a && source helm-charts/.env.dev && set +a && uv run pytest tests/integration/{spot,api_wired}/` (the `set -a` is required because `helm-charts/.env.dev` has no `export` prefixes).
- Never truncate integration test output (no `| tail`, `| head`, or piped filters) — always show complete pytest output.
