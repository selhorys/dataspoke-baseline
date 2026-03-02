# DataSpoke Baseline: AI Coding Scaffold

> **Document Status**: Specification v0.5 (updated 2026-02-27)
> This document covers Goal 2 of the DataSpoke Baseline project: providing a ready-to-use scaffold so that an organization-specific dedicated data catalog (a "Spoke") can be built with AI in a short time.
> Aligned with MANIFESTO v2 (user-group-based feature taxonomy).

---

## Table of Contents

1. [Purpose](#purpose)
2. [Scaffold Structure](#scaffold-structure)
3. [Utilities Catalog](#utilities-catalog)
   - [Skills](#skills)
   - [Commands](#commands)
   - [Subagents](#subagents)
4. [Permissions Model](#permissions-model)
5. [Current Status](#current-status)
6. [Building a Custom Spoke](#building-a-custom-spoke)
7. [Design Principles](#design-principles)

---

## Purpose

The DataSpoke Baseline pursues two goals (from `spec/MANIFESTO_en.md` §2):

1. **Baseline Product** — a pre-built implementation of essential features for an AI-era catalog, organized by user group: Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG).
2. **AI Scaffold** — sufficient conventions, development specs, and Claude Code utilities so that an organization-specific dedicated catalog can be built with AI in a short time.

This document covers **Goal 2**. The scaffold is the set of Claude Code configurations in `.claude/` that make AI-assisted development immediately productive from the first session.

**The core premise**: a well-structured scaffold removes the bootstrapping cost of AI coding — the AI agent knows the project layout, naming conventions, spec hierarchy, and operational environment before writing a single line of code.

The scaffold supports two categories of workflows (from MANIFESTO §4):

- **Development Environment Setup** — GitHub clone, reference data setup, and local Kubernetes cluster-based dev environment provisioning
- **Development Planning** — Guided spec authoring via `/dataspoke-plan-write`: scope selection → iterative Q&A → writing plan review → document writing (delegated to `plan-doc` conventions) → AI scaffold recommendations. Covers all spec tiers: architectural guidelines in `spec/`, common features in `spec/feature/`, and user-group-specific features in `spec/feature/spoke/` (DE/DA/DG). Implementation plans are tracked via GitHub Issues and PRs.

---

## Scaffold Structure

```
.claude/
├── skills/                     # Auto-loaded prompt extensions
│   ├── kubectl/                # Kubernetes operations against local cluster
│   ├── monitor-k8s/            # Cluster health reporting
│   ├── plan-doc/               # Spec document routing and authoring
│   ├── datahub-api/            # DataHub data model Q&A and code writing
│   ├── prauto-check-status/    # Prauto issue/PR status dashboard and heartbeat prediction
│   └── prauto-run-heartbeat/   # Heartbeat test-run with monitoring and self-healing
├── commands/                   # User-invoked multi-step workflows
│   ├── dataspoke-dev-env-install.md
│   ├── dataspoke-dev-env-uninstall.md
│   ├── dataspoke-ref-setup-all.md
│   └── dataspoke-plan-write.md
├── agents/                     # Subagent system prompts
│   ├── api-spec.md             # OpenAPI spec author
│   ├── backend.md              # FastAPI/Python implementer
│   ├── frontend.md             # Next.js/TypeScript implementer
│   └── k8s-helm.md             # Helm/Kubernetes/Docker author
├── hooks/                      # Automated guardrail scripts
│   └── auto-format.sh          # Auto-format Python/TypeScript on write (async)
├── settings.json               # Tool permissions + hook configuration
└── settings.local.json         # Local overrides (machine-specific approvals)

.prauto/
├── config.env                  # [COMMITTED] Shared settings
├── config.local.env            # [GITIGNORED] Instance-specific settings
├── heartbeat.sh                # [COMMITTED] Main cron entry point
├── lib/                        # Shell libraries (quota, issues, claude, git-ops, state)
├── prompts/                    # Prompt templates (analysis, implementation, squash-commit)
├── state/                      # [GITIGNORED] Runtime state (job, lock, logs, sessions)
└── README.md
```

The scaffold works alongside five other structural elements:
- **`CLAUDE.md`** — compact root-level agent instructions: project context, key design decisions, spec hierarchy, and implementation workflow. Points to `spec/ARCHITECTURE.md` and this document for details
- **`spec/`** — hierarchical specification documents (MANIFESTO → ARCHITECTURE → feature specs). Feature specs split into `spec/feature/` (common/cross-cutting) and `spec/feature/spoke/` (user-group-specific DE/DA/DG)
- **`dev_env/`** — local Kubernetes dev environment scripts
- **`ref/`** — external source code for AI reference (version-locked DataHub v1.4.0 OSS source, downloaded via `ref/setup.sh`)
- **`.prauto/`** — autonomous PR worker: cron-driven issue-to-PR automation via Claude Code CLI. See `spec/AI_PRAUTO.md` for full specification

---

## Utilities Catalog

### Skills

Skills are prompt extensions that give the agent specialized context or workflows for a specific domain. They live in `.claude/skills/` and are loaded when the user invokes them explicitly (`/skill-name`) or when Claude detects a matching context.

| Skill | Invocation | Scope | Purpose |
|-------|-----------|-------|---------|
| `kubectl` | `/kubectl <operation>` | User-invoked only | Run kubectl/helm operations against the local cluster; reads cluster name and namespaces from `dev_env/.env` |
| `monitor-k8s` | `/monitor-k8s [focus]` | User-invoked; runs in forked subagent | Full cluster health report: pod status, recent events, Helm releases |
| `plan-doc` | `/plan-doc <topic>` | User-invoked or auto-triggered when writing specs | Route spec authorship to the correct tier: `spec/feature/` for common features, `spec/feature/spoke/` for user-group-specific features (DE/DA/DG) |
| `datahub-api` | `/datahub-api <task>` | User-invoked or auto-triggered on DataHub API tasks | Dual-mode skill: Q&A mode for DataHub data model questions, Code Writer mode for writing/testing Python code against DataHub APIs. Uses `ref/github/datahub/` source and live cluster for validation |
| `prauto-check-status` | `/prauto-check-status [filter]` | User-invoked only | Status dashboard across all prauto lifecycle labels (ready/wip/review/done/failed); predicts what the next heartbeat will do based on current state. See `spec/AI_PRAUTO.md` |
| `prauto-run-heartbeat` | `/prauto-run-heartbeat` | User-invoked only | Monitored test-run of `.prauto/heartbeat.sh`; watches state files, reads log output, and diagnoses + fixes script errors across up to 3 retry cycles. See `spec/AI_PRAUTO.md` |

### Commands

Commands are user-invoked multi-step workflows — scripted sequences of agent actions that would otherwise require many manual steps. They live in `.claude/commands/`.

**`dataspoke-plan-write` is the primary entry point for all spec authoring.** It orchestrates the full workflow (scope selection, requirements gathering, plan review, writing, scaffold recommendations) while delegating the actual writing to `plan-doc` skill conventions. Power users who already know the target file and content can use `/plan-doc <topic>` directly.

| Command | Invocation | Purpose |
|---------|-----------|---------|
| `dataspoke-dev-env-install` | `/dataspoke-dev-env-install` | End-to-end dev environment setup: configure `dev_env/.env`, run preflight checks, execute `install.sh`, monitor pod readiness, report access URLs |
| `dataspoke-dev-env-uninstall` | `/dataspoke-dev-env-uninstall` | Controlled teardown: show current cluster state, confirm with user, run `uninstall.sh`, clean up orphaned PVs |
| `dataspoke-ref-setup-all` | `/dataspoke-ref-setup-all` | Download all AI reference materials: run `ref/setup.sh` in background and monitor until complete. Provides DataHub v1.4.0 source for the `datahub-api` skill |
| `dataspoke-plan-write` | `/dataspoke-plan-write` | Guided spec authoring: select scope (architectural / common feature / spoke feature), gather requirements through iterative Q&A, review writing plan, write document using plan-doc conventions, recommend AI scaffold updates |

### Subagents

Subagents are specialized Claude instances with focused system prompts. The main agent delegates to them automatically when the task context matches. They live in `.claude/agents/`.

| Subagent | Trigger context | Tools | Scope |
|----------|----------------|-------|-------|
| `api-spec` | Designing or writing OpenAPI 3.0 specs in `api/` | Read, Write, Edit, Glob, Grep | API-first design; outputs YAML specs + companion markdown. Specs follow user-group URI routing (`/api/v1/spoke/[de\|da\|dg]/...`) |
| `backend` | Implementing FastAPI/Python in `src/api/`, `src/backend/`, `src/workflows/`, `src/shared/` | Read, Write, Edit, Glob, Grep, Bash | Backend services for all user groups (DE/DA/DG). Can run `pytest`, `python3`, `alembic`, `pip` to verify its own work |
| `frontend` | Implementing Next.js/TypeScript in `src/frontend/` | Read, Write, Edit, Glob, Grep, Bash | Portal-style UI with user-group entry points (DE, DA, DG). Can run `npm`, `npx`, `jest`, `tsc` to verify its own work |
| `k8s-helm` | Writing Helm charts, Dockerfiles, or dev env scripts | Read, Write, Edit, Glob, Grep, Bash | Container images, K8s manifests, Helm chart templates |

`backend` and `frontend` subagents have Bash access so they can run tests and type-checks to verify their own output before reporting completion.

---

## Permissions Model

Defined in `.claude/settings.json`. The guiding principle: **read freely, mutate with confirmation, never destroy**.

| Category | Policy |
|----------|--------|
| Read-only operations (`kubectl get`, `helm list`, `git log`, `docker ps`) | Auto-allowed |
| Mutating operations (`kubectl apply`, `helm install`, `helm upgrade`, `kubectl rollout`) | Prompt for confirmation |
| Destructive operations (`kubectl delete namespace`, `rm -rf`, `sudo`) | Always blocked |

This allows the agent to freely inspect the local cluster state while requiring explicit user approval before changing it.

---

## Current Status

The scaffold has been **refactored for implementation readiness** (v0.5, 2026-02-27). Key changes from v0.4:

- **CLAUDE.md slimmed** from ~250 lines to ~60 lines — duplicated architecture/layout content removed, points to `spec/ARCHITECTURE.md` for details
- **Hooks reduced** from 5 to 1 — only `auto-format.sh` remains. Commit validation, MANIFESTO protection, spec propagation reminders, and post-compaction context injection are now handled by CLAUDE.md instructions (lower overhead, same effect)
- **Subagent tool access fixed** — `backend` and `frontend` agents now have Bash access for running tests, type-checks, and build tools
- **Implementation workflow documented** in CLAUDE.md — standard subagent sequencing for end-to-end feature implementation (api-spec → backend → frontend → k8s-helm)

### Scaffold components

| Component | Status |
|-----------|--------|
| `.claude/` scaffold (skills, commands, agents, settings) | Complete (v0.5) |
| `spec/` hierarchy (MANIFESTO → ARCHITECTURE → feature) | Complete |
| `dev_env/` local Kubernetes environment (DataHub + example sources) | Complete |
| `ref/` AI reference materials (DataHub v1.4.0 source) | Complete |
| `api/` standalone OpenAPI specs | Initial spec created (`openapi.yaml`) |
| `src/` application source code | Not yet created |
| `helm-charts/` deployment packaging | Umbrella chart with subcharts created |

### Spec inventory

Documents authored so far (use `/dataspoke-plan-write` to add more):

| Tier | Document | Description |
|------|----------|-------------|
| Top-level | `spec/MANIFESTO_en.md`, `spec/MANIFESTO_kr.md` | Product identity, user-group taxonomy (DE/DA/DG). Highest authority |
| Top-level | `spec/ARCHITECTURE.md` | System-wide architecture, components, tech stack, feature mapping (UC1–UC8) |
| Top-level | `spec/TESTING.md` | Testing conventions: toolchain, unit/integration/E2E patterns, Imazon test data design |
| Top-level | `spec/AI_SCAFFOLD.md` | This document — Claude Code scaffold conventions |
| Top-level | `spec/AI_PRAUTO.md` | Prauto: autonomous PR worker specification |
| Top-level | `spec/USE_CASE_en.md`, `spec/USE_CASE_kr.md` | Conceptual scenarios by user group (UC1–UC8) |
| Top-level | `spec/DATAHUB_INTEGRATION.md` | DataHub SDK patterns, aspect catalog, error handling |
| Top-level | `spec/API_DESIGN_PRINCIPLE_en.md`, `spec/API_DESIGN_PRINCIPLE_kr.md` | REST API conventions |
| Common feature | `spec/feature/API.md` | API layer design and shared infrastructure |
| Common feature | `spec/feature/DEV_ENV.md` | Local Kubernetes dev environment specification |
| Common feature | `spec/feature/HELM_CHART.md` | DataSpoke umbrella Helm chart for Kubernetes deployment |
| Spoke feature | *(none yet)* | User-group-specific feature specs — next authoring target |
**Next steps**: Author user-group-specific feature specs in `spec/feature/spoke/` using `/dataspoke-plan-write` (scope 3). The MANIFESTO defines the following features awaiting specs: Deep Technical Spec Ingestion (DE), Online Data Validator (DE/DA), Automated Documentation Generation (DE), Natural Language Search (DA), Text-to-SQL Optimized Metadata (DA), Enterprise Metrics Time-Series Monitoring (DG), Multi-Perspective Data Overview (DG).

The project is transitioning from the **specification phase to the implementation phase**. Specs and dev environment are complete; the scaffold is now optimized for implementation workflows (subagent sequencing, self-verification, reduced context overhead).

---

## Building a Custom Spoke

The scaffold is designed to be forked and adapted for an organization's specific needs. A custom Spoke is a DataSpoke implementation tailored to the organization's data sources, domain vocabulary, user groups, and operational requirements.

### Typical customization points

```
Fork dataspoke-baseline
│
├── spec/MANIFESTO_*.md         ← Redefine user groups, features, and product identity
├── spec/ARCHITECTURE.md        ← Adjust stack choices (e.g. Airflow over Temporal)
├── spec/feature/               ← Common/cross-cutting feature specs
├── spec/feature/spoke/         ← User-group-specific feature specs (DE/DA/DG)
│
├── src/api/routers/            ← Add/modify user-group API routers
├── src/backend/                ← Implement features for your user groups
│
├── dev_env/.env                ← Point to your cluster and namespaces
└── .claude/agents/backend.md   ← Extend with org-specific conventions
```

### Recommended sequence

1. **Revise the manifesto** — redefine user groups (DE/DA/DG or your own), feature scope, and naming for your org
2. **Run `/dataspoke-plan-write`** (scope 1: architectural) — update `ARCHITECTURE.md` with adjusted tech stack, system components, and integration points
3. **Run `/dataspoke-plan-write`** (scope 2: common feature) — define cross-cutting feature specs in `spec/feature/` (e.g., API layer, shared services)
4. **Run `/dataspoke-plan-write`** (scope 3: spoke feature) — define user-group-specific specs in `spec/feature/spoke/` for each DE/DA/DG feature
5. **Run `/dataspoke-dev-env-install`** — bring up the local DataHub environment
6. **Use `api-spec` subagent** — design user-group API contracts (`/api/v1/spoke/[group]/...`) per the feature specs
7. **Use `backend` subagent** — implement feature services iteratively
8. **Use `frontend` subagent** — build the portal-style UI with user-group entry points
9. **Use `k8s-helm` subagent** — package and deploy to your target environment

Steps 2–4 use `/dataspoke-plan-write` to ensure every spec follows MANIFESTO conventions, uses the correct template, and includes cross-references. The command's Step 7 (AI scaffold recommendations) also identifies when new subagents, skills, or permissions are needed before implementation begins.

### What the scaffold saves

| Without scaffold | With scaffold |
|-----------------|--------------|
| Agent must learn project layout from scratch each session | `CLAUDE.md` + spec hierarchy provides immediate context |
| Spec authoring requires knowing the hierarchy, templates, and naming rules upfront | `/dataspoke-plan-write` guides the user through scope → Q&A → plan review → writing → scaffold recommendations |
| No standard for spec documents → inconsistent output | `plan-doc` skill (invoked by `dataspoke-plan-write`) enforces spec hierarchy and format |
| Manual cluster setup and teardown | `dataspoke-dev-env-install/uninstall` commands handle it end-to-end |
| Risk of agent running destructive commands | `settings.json` permission rules block them |
| API and backend developed in parallel without contract | `api-spec` subagent establishes the contract first |

---

## Design Principles

### 1. Context before code
The agent reads the spec hierarchy (MANIFESTO → ARCHITECTURE → feature specs) before generating any implementation. `CLAUDE.md` is the entry point that orients the agent to the full project state.

### 2. Spec as the source of truth
All naming, user-group taxonomy (DE/DA/DG), and product identity derive from `MANIFESTO_en.md`. Subagents are instructed to consult it before making naming decisions. The `plan-doc` skill routes new documentation to the correct tier automatically.

### 3. User-group-driven organization
Features, API routes, and UI entry points are organized by user group (DE, DA, DG) rather than by technical component. This mirrors the MANIFESTO's structure and ensures that each Spoke serves its target audience clearly.

### 4. API-first development
The `api-spec` subagent produces OpenAPI specs as standalone artifacts before backend implementation begins. Specs follow the three-tier URI pattern: `/api/v1/spoke/common/…` for shared features, `/api/v1/spoke/[de|da|dg]/…` for user-group features, `/api/v1/hub/…` for DataHub pass-through. This allows frontend and backend subagents to work from a shared contract without requiring a running service.

### 5. Least privilege for agent tools
The permissions model is conservative by default. Agents can read and inspect freely but cannot make changes to shared or persistent state without user confirmation. This is especially important for cluster operations.

### 6. Self-verifying subagents
`backend` and `frontend` subagents have Bash access to run tests and type-checks. They verify their own output before reporting completion, catching errors early without requiring the main agent to re-check.
