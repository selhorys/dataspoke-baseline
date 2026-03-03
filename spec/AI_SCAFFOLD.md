# DataSpoke AI Coding Scaffold

## Table of Contents

1. [Purpose](#purpose)
2. [Scaffold Structure](#scaffold-structure)
3. [Skills](#skills)
4. [Subagents](#subagents)
5. [Permissions](#permissions)
6. [Hooks](#hooks)
7. [Prauto](#prauto)
8. [Building a Custom Spoke](#building-a-custom-spoke)
9. [Design Principles](#design-principles)

---

## Purpose

The DataSpoke Baseline pursues two goals (from `spec/MANIFESTO_en.md` §2):

1. **Baseline Product** — a pre-built implementation of essential features for an AI-era catalog, organized by user group: Data Engineers (DE), Data Analysts (DA), and Data Governance personnel (DG).
2. **AI Scaffold** — sufficient conventions, development specs, and Claude Code utilities so that an organization-specific dedicated catalog can be built with AI in a short time.

This document covers **Goal 2**. The scaffold is the set of Claude Code configurations in `.claude/` that make AI-assisted development immediately productive from the first session. A well-structured scaffold removes the bootstrapping cost of AI coding — the AI agent knows the project layout, naming conventions, spec hierarchy, and operational environment before writing a single line of code.

---

## Scaffold Structure

```
.claude/
├── skills/                     # Prompt extensions and multi-step workflows
│   ├── k8s-work/               # Kubernetes cluster management (health, monitoring, operations)
│   ├── plan-doc/               # Spec document routing and authoring
│   ├── datahub-api/            # DataHub data model Q&A and code writing
│   ├── prauto-check-status/    # Prauto issue/PR status dashboard
│   ├── prauto-run-heartbeat/   # Heartbeat test-run with monitoring and self-healing
│   ├── dev-env/                # Dev environment management (install, uninstall, port-forward, status)
│   ├── ref-setup/              # Download AI reference materials
│   └── sync-spec-to-impl/     # Spec ↔ implementation synchronization
├── agents/                     # Subagent system prompts (model: sonnet)
│   ├── api-spec.md             # OpenAPI spec author
│   ├── backend.md              # FastAPI/Python implementer
│   ├── frontend.md             # Next.js/TypeScript implementer
│   └── k8s-helm.md             # Helm/Kubernetes/Docker author
├── hooks/
│   └── auto-format.sh          # PostToolUse: auto-format Python (ruff) / TypeScript (prettier)
├── settings.json               # Tool permissions + hook configuration
└── settings.local.json         # Local overrides (machine-specific approvals)
```

The scaffold works alongside these structural elements:

| Element | Role |
|---------|------|
| `CLAUDE.md` | Root-level agent instructions: project context, spec hierarchy, implementation workflow |
| `spec/` | Hierarchical spec documents (MANIFESTO → ARCHITECTURE → feature specs) |
| `dev_env/` | Local Kubernetes dev environment scripts. See `spec/feature/DEV_ENV.md` |
| `ref/` | External source code for AI reference (DataHub v1.4.0 source, downloaded via `/ref-setup`) |
| `.prauto/` | Autonomous PR worker: cron-driven issue-to-PR automation. See `spec/AI_PRAUTO.md` |
| `api/` | Consolidated OpenAPI spec (`openapi.yaml`) |
| `helm-charts/` | DataSpoke umbrella Helm chart with subcharts. See `spec/feature/HELM_CHART.md` |

---

## Skills

Skills are prompt extensions that give the agent specialized context for a specific domain. They live in `.claude/skills/<name>/SKILL.md` and are loaded when invoked explicitly (`/skill-name`) or when Claude detects a matching context.

| Skill | Purpose |
|-------|---------|
| `k8s-work` | Kubernetes cluster management: one-time health checks, continuous monitoring with polling during installs, and kubectl/helm operations. Runs as a forked subagent; reads cluster config from `dev_env/.env` |
| `plan-doc` | Route spec authorship to the correct tier (`spec/feature/` or `spec/feature/spoke/`) using the project's template and naming conventions |
| `datahub-api` | Dual-mode: Q&A about DataHub's data model, or write/test Python code against DataHub APIs. Uses `ref/github/datahub/` source and live cluster. Requires `/ref-setup` first |
| `prauto-check-status` | Status dashboard across all prauto lifecycle labels; predicts what the next heartbeat will do |
| `prauto-run-heartbeat` | Monitored test-run of `.prauto/heartbeat.sh`; watches state files, reads logs, diagnoses + fixes script errors across up to 3 retry cycles |
| `dev-env` | Dev environment management: install (full or partial), uninstall (full or partial), start/stop port-forwarding, cluster status check. Accepts action + optional component list as arguments |
| `ref-setup` | Download AI reference materials (DataHub v1.4.0 source) with interactive selection; monitor in background until complete |
| `sync-spec-to-impl` | Compare specification documents against current implementation, identify drift, and reconcile. Supports scoped sync (prauto, ai-scaffold, dev-env, helm-charts, api, ref) or full sync across all scopes |

Each skill's SKILL.md is the authoritative reference for its behavior, invocation options, and allowed tools.

---

## Subagents

Subagents are specialized Claude instances with focused system prompts. The main agent delegates to them when the task context matches. They live in `.claude/agents/` and all use the `sonnet` model.

| Subagent | Scope | Tools |
|----------|-------|-------|
| `api-spec` | OpenAPI 3.0 specs in `api/openapi.yaml` (single consolidated file). Follows URI routing: `/spoke/common/`, `/spoke/[de\|da\|dg]/`, `/hub/` | Read, Write, Edit, Glob, Grep |
| `backend` | FastAPI/Python code in `src/api/`, `src/backend/`, `src/workflows/`, `src/shared/`. Runs `pytest` to self-verify | Read, Write, Edit, Glob, Grep, Bash |
| `frontend` | Next.js/TypeScript code in `src/frontend/`. Runs `npm test` and `tsc` to self-verify | Read, Write, Edit, Glob, Grep, Bash |
| `k8s-helm` | Helm charts, Dockerfiles, Kubernetes manifests, dev environment scripts | Read, Write, Edit, Glob, Grep, Bash |

The standard implementation workflow sequences these agents: spec → `api-spec` → `backend` → `frontend` → `k8s-helm`. Each reads the spec and the output of previous agents as context. See `CLAUDE.md` §Implementation Workflow.

---

## Permissions

Defined in `.claude/settings.json`. The guiding principle: **read freely, mutate with confirmation, never destroy**.

| Category | Policy | Examples |
|----------|--------|----------|
| Read-only | Auto-allowed | `kubectl get`, `helm list`, `git log`, `docker ps` |
| Reference docs | Auto-allowed | `WebSearch`, `WebFetch` to framework/tool documentation domains |
| Skills | Auto-allowed / prompt | Most skills auto-allowed; `prauto-run-heartbeat` and `ref-setup` require user confirmation (side effects) |
| Dev env scripts | Auto-allowed | `bash dev_env/install.sh`, `bash dev_env/uninstall.sh` |
| Mutating | Prompt for confirmation | `kubectl apply`, `helm install`, `helm upgrade` |
| Destructive | Always blocked | `kubectl delete namespace`, `rm -rf`, `sudo` |

The full allow/deny lists are in `.claude/settings.json`. The settings file is the authoritative reference.

---

## Hooks

A single async hook (`auto-format.sh`) runs after every `Edit` or `Write` tool call:

- **Python files** (`.py`): `ruff check --fix` + `ruff format`
- **TypeScript files** (`.ts`, `.tsx`): `npx prettier --write`

Formatting is best-effort — if `ruff` or `npx` is not installed, the hook silently exits. The hook is async (non-blocking) with a 15-second timeout.

---

## Prauto

Prauto is the autonomous PR worker — a cron-driven system that picks up GitHub issues labeled `prauto:ready`, produces implementation PRs via Claude Code CLI, and manages the full issue-to-PR lifecycle. It lives in `.prauto/`.

```
.prauto/
├── config.env                  # [COMMITTED] Shared settings (repo, labels, branch prefix)
├── config.local.env            # [GITIGNORED] Instance-specific settings (tokens, worker ID)
├── config.local.env.example    # Template for config.local.env
├── heartbeat.sh                # [COMMITTED] Main cron entry point
├── lib/                        # Shell libraries
│   ├── helpers.sh              #   Logging, config loading
│   ├── state.sh                #   Job state, locking
│   ├── quota.sh                #   Token quota management
│   ├── issues.sh               #   Issue discovery, claiming
│   ├── claude.sh               #   Claude CLI invocation
│   ├── git-ops.sh              #   Branch creation, worktree, push
│   ├── pr.sh                   #   PR creation, feedback, squash-finalize
│   └── phases.sh               #   Phase-specific handlers
├── prompts/                    # Prompt templates for Claude CLI invocations
│   ├── issue-analysis.md       #   Issue analysis and plan generation
│   ├── implementation.md       #   Code implementation
│   ├── squash-commit.md        #   Squash-finalize commit message
│   └── system-append.md        #   System prompt supplement
├── state/                      # [GITIGNORED] Runtime state
│   ├── current-job.json        #   Active job (issue, phase, retries, branch)
│   ├── heartbeat.lock          #   PID-based lock file
│   ├── sessions/               #   Analysis, implementation, review outputs
│   └── history/                #   Completed job records
└── README.md
```

See `spec/AI_PRAUTO.md` for the full specification (lifecycle labels, heartbeat decision tree, plan-approval protocol, squash-finalize workflow).

---

## Building a Custom Spoke

The scaffold is designed to be forked and adapted. A custom Spoke is a DataSpoke implementation tailored to an organization's data sources, domain vocabulary, user groups, and operational requirements.

### Typical customization points

| What to customize | Where |
|-------------------|-------|
| User groups, features, product identity | `spec/MANIFESTO_*.md` |
| Tech stack, system components | `spec/ARCHITECTURE.md` |
| Common feature specs | `spec/feature/` |
| User-group-specific feature specs | `spec/feature/spoke/` |
| API routers and backend services | `src/api/`, `src/backend/` |
| Cluster and namespace config | `dev_env/.env` |
| Org-specific agent conventions | `.claude/agents/` |

### Recommended sequence

1. **Revise the manifesto** — redefine user groups and feature scope
2. **Run `/plan-doc`** — update architectural specs, then common and spoke feature specs
3. **Run `/dev-env install`** — bring up the local DataHub environment
4. **Use subagents** in order: `api-spec` → `backend` → `frontend` → `k8s-helm`

Steps 1-2 ensure every spec follows MANIFESTO conventions.

---

## Design Principles

1. **Context before code** — The agent reads the spec hierarchy (MANIFESTO → ARCHITECTURE → feature specs) before generating implementation. `CLAUDE.md` is the entry point that orients the agent.

2. **Spec as the source of truth** — All naming and user-group taxonomy derive from `MANIFESTO_en.md`. The `plan-doc` skill routes new documents to the correct tier automatically.

3. **User-group-driven organization** — Features, API routes, and UI entry points are organized by user group (DE, DA, DG), mirroring the MANIFESTO's structure.

4. **API-first development** — The `api-spec` subagent produces OpenAPI specs before backend implementation begins, following the three-tier URI pattern.

5. **Least privilege** — Agents read and inspect freely but cannot change shared state without user confirmation. Destructive cluster operations are blocked.

6. **Self-verifying subagents** — `backend` and `frontend` agents have Bash access to run tests and type-checks, catching errors before reporting completion.
