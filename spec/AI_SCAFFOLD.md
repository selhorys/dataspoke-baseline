# DataSpoke AI Coding Scaffold

## Table of Contents

1. [Purpose](#purpose)
2. [Scaffold Structure](#scaffold-structure)
3. [Skills](#skills)
4. [Subagents](#subagents)
5. [Permissions](#permissions)
6. [Prauto](#prauto)
7. [Building a Custom Spoke](#building-a-custom-spoke)
8. [Design Principles](#design-principles)

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
│   ├── kestra-api/             # Kestra REST API reference and code writing
│   ├── prauto-check-status/    # Prauto issue/PR status dashboard
│   ├── prauto-run-heartbeat/   # Heartbeat test-run with monitoring and self-healing
│   ├── dev-env/                # Dev environment management (configure, install, port-forward, health-check, run-dataspoke-test-mode, uninstall)
│   ├── ref-setup/              # Download AI reference materials
│   ├── sync-spec-from-impl/     # Spec ↔ implementation synchronization
│   ├── sync-specs/            # Forward spec propagation (spec → sibling/parent specs)
│   └── spec-to-bulk-issue/    # Bulk-create implementation issues from specs
├── agents/                     # Subagent system prompts
│   ├── architect.md            # Implementation planner (opus) — codebase analysis + blueprints
│   ├── reviewer.md             # Independent evaluator (opus) — spec compliance + quality scoring
│   ├── backend.md              # FastAPI/Python implementer (sonnet)
│   ├── workflow.md             # Kestra flow YAML + workflow helper module implementer (sonnet)
│   ├── test.md                 # Test writer and runner (sonnet)
│   ├── frontend.md             # Next.js/TypeScript implementer (sonnet)
│   └── k8s-helm.md             # Helm/Kubernetes/Docker author (sonnet)
├── settings.json               # Tool permissions
└── settings.local.json         # Local overrides (machine-specific approvals)
```

The scaffold works alongside these structural elements:

| Element | Role |
|---------|------|
| `CLAUDE.md` | Root-level agent instructions: project context, spec hierarchy, implementation workflow |
| `spec/` | Hierarchical spec documents (MANIFESTO → ARCHITECTURE → feature specs) |
| `dev_env/` | Kubernetes dev environment scripts. See `spec/feature/DEV_ENV.md` |
| `ref/` | External source code for AI reference (DataHub v1.4.0.3, Kestra source, downloaded via `/ref-setup`) |
| `.prauto/` | Autonomous PR worker: cron-driven issue-to-PR automation. See `spec/AI_PRAUTO.md` |
| `helm-charts/` | DataSpoke umbrella Helm chart with subcharts. See `spec/feature/HELM_CHART.md` |

---

## Skills

Skills are prompt extensions that give the agent specialized context for a specific domain. They live in `.claude/skills/<name>/SKILL.md` and are loaded when invoked explicitly (`/skill-name`) or when Claude detects a matching context.

| Skill | Purpose |
|-------|---------|
| `k8s-work` | Kubernetes cluster management: one-time health checks, continuous monitoring with polling during installs, and kubectl/helm operations. Runs as a forked subagent; reads cluster config from `dev_env/.env` |
| `plan-doc` | Write specification or planning documents in `spec/` following the project hierarchy and naming conventions |
| `datahub-api` | Reference and coding guide for DataHub integration in backend development. Covers entities, aspects, lineage, URNs, ingestion/emission, GraphQL, REST, and the `acryl-datahub` SDK. Requires `/ref-setup` first |
| `kestra-api` | Reference and coding guide for Kestra REST API integration in workflow development. Covers flows, executions, logs, KV store, triggers, and the KestraClient wrapper. Requires `/ref-setup` first |
| `prauto-check-status` | Status dashboard across all prauto lifecycle labels; predicts what the next heartbeat will do |
| `prauto-run-heartbeat` | Monitored test-run of `.prauto/heartbeat.sh`; watches state files, reads logs, diagnoses + fixes script errors across up to 3 retry cycles |
| `dev-env` | Dev environment management: configure, install (full or partial), uninstall (full or partial), start/stop port-forwarding, health-check, and run-dataspoke-test-mode (host-mode app via `uv run -m src.cli`). Accepts action + optional component/options as arguments |
| `ref-setup` | Download AI reference materials (external source code for AI assistant reference) with interactive selection; monitor in background until complete |
| `sync-spec-from-impl` | Reverse-sync specs from implementation (impl → spec). Detects structural drift, naming mismatches, undocumented features, and stale references. Supports scoped sync (prauto, ai-scaffold, dev-env, helm-charts, api, ref, backend, frontend) or full sync |
| `sync-specs` | Propagate spec changes to sibling/parent specs and harness docs. When a spec is created, modified, or deleted, updates all documents that reference or list it |
| `spec-to-bulk-issue` | Analyze specs to find unimplemented components, write ordered issue tickets in `issues/`, revise existing issues, and optionally register them to GitHub with `prauto:ready` label |

Each skill's SKILL.md is the authoritative reference for its behavior, invocation options, and allowed tools.

---

## Subagents

Subagents are specialized Claude instances with focused system prompts. They live in `.claude/agents/` and are organized into three roles following the **planner → generator → evaluator** pattern (see §Design Principles).

### Planner and Evaluator (opus model)

| Subagent | Role | Scope | Tools |
|----------|------|-------|-------|
| `architect` | Planner | Analyzes codebase + feature specs to produce implementation blueprints with file lists, component boundaries, data flows, and acceptance criteria. Invoked before generators for non-trivial features | Read, Glob, Grep, Bash |
| `reviewer` | Evaluator | Independently reviews generator output against spec + architect's plan. Produces structured pass/fail scoring across 5 criteria (spec compliance, architecture adherence, code quality, completeness, inter-component consistency). Invoked after each generator | Read, Glob, Grep, Bash |

Both use read-only tools — they analyze and report but do not write code.

### Generators (sonnet model)

| Subagent | Scope | Tools |
|----------|-------|-------|
| `backend` | FastAPI routes, services, shared libs in `src/api/`, `src/backend/`, `src/shared/`. Reads feature specs and architect's plan. Self-verifies with `pytest`. Supports fix pass mode for reviewer findings | Read, Write, Edit, Glob, Grep, Bash |
| `workflow` | Kestra flow YAML in `src/workflows/flows/` and workflow parameter modules. Orchestrates `src/backend/` services via HTTP Request tasks. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `test` | Tests across all layers in `tests/`. Follows `spec/TESTING.md`. Supports reviewer-directed testing mode to verify specific findings | Read, Write, Edit, Glob, Grep, Bash |
| `frontend` | Next.js/TypeScript code in `src/frontend/`. Reads `FRONTEND_*.md` specs. Self-verifies with `npm test` and `tsc`. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `k8s-helm` | Helm charts, Dockerfiles, Kubernetes manifests, dev environment scripts. No review loop (infrastructure changes are lower-risk) | Read, Write, Edit, Glob, Grep, Bash |

### Implementation workflow

The standard workflow uses the planner → generator → evaluator loop:

```
1. Read spec
2. architect → implementation plan with acceptance criteria
3. backend → reviewer → [fix pass if REVISE, max 1 iteration]
4. workflow → reviewer → [fix pass if REVISE, max 1 iteration]
5. test (writes + runs tests; can verify reviewer findings)
6. frontend → reviewer → [fix pass if REVISE, max 1 iteration]
7. k8s-helm (when ready, no review loop)
```

The main agent orchestrates by passing context between agents: architect's plan feeds into generators, generator completion reports feed into the reviewer, reviewer findings feed back to generators for fix passes. If issues persist after one fix pass, they are escalated to the user.

See `CLAUDE.md` §Implementation Workflow for the authoritative reference.

---

## Permissions

Defined in `.claude/settings.json`. The guiding principle: **read freely, mutate with confirmation, never destroy**.

| Category | Policy | Examples |
|----------|--------|----------|
| Read-only | Auto-allowed | `kubectl get`, `helm list`, `git log`, `docker ps` |
| Reference docs | Auto-allowed | `WebSearch`, `WebFetch` to framework/tool documentation domains |
| Skills | Auto-allowed / prompt | Most skills auto-allowed; `prauto-run-heartbeat`, `ref-setup`, and `spec-to-bulk-issue` require user confirmation (side effects) |
| Dev env scripts | Auto-allowed | `bash dev_env/install.sh`, `bash dev_env/uninstall.sh` |
| Mutating | Prompt for confirmation | `kubectl apply`, `helm install`, `helm upgrade` |
| Destructive | Always blocked | `kubectl delete namespace`, `rm -rf`, `sudo` |

The full allow/deny lists are in `.claude/settings.json`. The settings file is the authoritative reference.

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
│   ├── system-append.md        #   System prompt supplement
│   ├── issue-analysis.md       #   Issue analysis and plan generation
│   ├── implementation.md       #   Code implementation
│   ├── code-review.md          #   Independent code review (generator-evaluator pattern)
│   ├── review-fix.md           #   Address code review findings
│   ├── integration-fix.md      #   Fix integration test failures
│   ├── pr-review.md            #   Address PR reviewer feedback
│   ├── feedback-response.md    #   Respond to plan counter-proposal
│   └── squash-commit.md        #   Squash-finalize commit message
├── state/                      # [GITIGNORED] Runtime state
│   ├── heartbeat.lock          #   PID-based lock file
│   ├── heartbeat.log           #   Cron output log
│   ├── .system-append-rendered.md
│   └── sessions/               #   Per-issue session outputs (analysis, implementation, review)
├── worktrees/                  # [GITIGNORED] Git worktrees for active jobs
└── README.md
```

See `spec/AI_PRAUTO.md` for the full specification (lifecycle labels, heartbeat decision tree, plan-approval protocol, code review phase, squash-finalize workflow).

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
3. **Run `/dev-env install`** — bring up the DataHub environment
4. **Use subagents** in the planner → generator → evaluator workflow: `architect` → `backend` → `reviewer` → `test` → `frontend` → `reviewer` → `k8s-helm`

Steps 1-2 ensure every spec follows MANIFESTO conventions.

---

## Design Principles

1. **Context before code** — The agent reads the spec hierarchy (MANIFESTO → ARCHITECTURE → feature specs) before generating implementation. `CLAUDE.md` is the entry point that orients the agent.

2. **Spec as the source of truth** — All naming and user-group taxonomy derive from `MANIFESTO_en.md`. The `plan-doc` skill routes new documents to the correct tier automatically.

3. **User-group-driven organization** — Features, API routes, and UI entry points are organized by user group (DE, DA, DG), mirroring the MANIFESTO's structure.

4. **API-first development** — The `backend` subagent implements API routes as the single source of truth for the API contract, following the three-tier URI pattern defined in feature specs. FastAPI auto-generates OpenAPI documentation from the implementation.

5. **Least privilege** — Agents read and inspect freely but cannot change shared state without user confirmation. Destructive cluster operations are blocked.

6. **Self-verifying subagents** — `backend`, `workflow`, `frontend`, and `test` agents have Bash access to run tests and type-checks, catching errors before reporting completion. Self-verification is necessary but not sufficient — it is complemented by independent review (see principle 8).

7. **Separation of concerns** — Each subagent has a focused scope. Backend routes and services are separate from Kestra workflow definitions. Testing is a first-class agent activity, not an afterthought appended to implementation agents.

8. **Generator-evaluator separation** — Generators (backend, workflow, frontend) write code and self-test. An independent `reviewer` agent evaluates the output against the spec and architect's plan. Self-evaluation is insufficient for quality assurance — models tend to praise their own work. External critique from a separate context is a stronger signal. (Source: [Anthropic harness design research](https://www.anthropic.com/engineering/harness-design-long-running-apps).)

9. **Model-appropriate roles** — Use stronger models (opus) for roles requiring judgment and reasoning (`architect`, `reviewer`). Use faster models (sonnet) for volume code generation. Evaluators need the strongest available model to resist self-praise patterns and produce genuinely critical assessments.

10. **Bounded iteration** — Review loops are capped at 1 fix iteration per generator to control cost and latency. Unresolved issues after one fix pass are escalated to the user rather than looping indefinitely.
