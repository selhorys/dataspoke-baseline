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

`.claude/` contains: `skills/` (prompt extensions — one directory per skill), `agents/` (subagent system prompts — one `.md` per agent), `settings.json` (tool permissions), and `settings.local.json` (local overrides). See §Skills and §Subagents below for the full catalogue.

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
| `dev-env` | Dev environment management: configure, install (full or partial), reinstall (selective component reset with PVC + DB cleanup), uninstall (full or partial), health-check, and run-dataspoke-test-mode (build + deploy in-cluster API via Helm). Services are accessed via nginx-ingress — no port-forwarding. Accepts action + optional component/options as arguments |
| `ref-setup` | Download AI reference materials (external source code for AI assistant reference) with interactive selection; monitor in background until complete |
| `spec-sync-from-impl` | Reverse-sync specs from implementation (impl → spec). Detects structural drift, naming mismatches, undocumented features, and stale references. Supports scoped sync (prauto, ai-scaffold, dev-env, helm-charts, api, ref, backend, frontend) or full sync |
| `spec-harmonize` | Propagate spec changes to sibling/parent specs and harness docs. When a spec is created, modified, or deleted, updates all documents that reference or list it |
| `spec-reduce` | Audit and trim bloated specs, scaffold docs, and READMEs. Removes implementation details, eliminates cross-tier duplication, enforces abstraction-level discipline |
| `spec-to-bulk-issue` | Analyze specs to find unimplemented components, write ordered issue tickets in `issues/`, revise existing issues, and optionally register them to GitHub with `prauto:ready` label |

Each skill's SKILL.md is the authoritative reference for its behavior, invocation options, and allowed tools.

---

## Subagents

Subagents are specialized Claude instances with focused system prompts. They live in `.claude/agents/` and are organized into two roles following the **generator → evaluator** pattern (see §Design Principles). Planning is handled by Claude's built-in Plan mode before generators are invoked.

**Why subagents**: The primary reason to delegate implementation to subagents is **context confinement**. Each generator operates in a fresh, focused context — it sees only the approved plan, the relevant spec, and the files in its scope. This prevents the main conversation from accumulating implementation noise (hundreds of lines of generated code, test output, linter errors) that degrades the quality of subsequent decisions. The main agent stays clean for orchestration: passing plans, routing reviewer findings, and deciding what to run next. For non-trivial implementation, delegate to the appropriate generator agent rather than writing code directly in the main conversation.

### Evaluator (opus model)

| Subagent | Role | Scope | Tools |
|----------|------|-------|-------|
| `reviewer` | Evaluator | Independently reviews generator output against spec + implementation plan. Produces structured pass/fail scoring across 5 criteria (spec compliance, architecture adherence, code quality, completeness, inter-component consistency). Invoked after each generator | Read, Glob, Grep, Bash |

The reviewer uses read-only tools — it analyzes and reports but does not write code.

### Generators (sonnet model)

| Subagent | Scope | Tools |
|----------|-------|-------|
| `backend` | FastAPI routes, services, shared libs in `src/api/`, `src/backend/`, `src/shared/`. Reads feature specs and the approved plan. Self-verifies with `pytest`. Supports fix pass mode for reviewer findings | Read, Write, Edit, Glob, Grep, Bash |
| `workflow` | Kestra flow YAML in `src/workflows/flows/` and workflow parameter modules. Orchestrates `src/backend/` services via HTTP Request tasks. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `test` | Tests across all layers in `tests/`. Follows `spec/TESTING.md`. Supports reviewer-directed testing mode to verify specific findings | Read, Write, Edit, Glob, Grep, Bash |
| `frontend` | Next.js/TypeScript code in `src/frontend/`. Reads `FRONTEND_*.md` specs. Self-verifies with `npm test` and `tsc`. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `k8s-helm` | Helm charts, Dockerfiles, Kubernetes manifests, dev environment scripts. No review loop (infrastructure changes are lower-risk) | Read, Write, Edit, Glob, Grep, Bash |

### Implementation workflow

The standard workflow uses the **plan → approve → generate → evaluate** pattern:

```
1. Plan    — Read the relevant spec, then use Claude's built-in Plan mode
             to produce an implementation plan. See §Plan quality checklist below.
2. Approve — Human reviews the plan and approves (or iterates interactively).
3. Generate + Evaluate:
   a. backend  → reviewer → [fix pass if REVISE, max 1 iteration]
   b. workflow → reviewer → [fix pass if REVISE, max 1 iteration]
   c. test (writes + runs tests; can verify reviewer findings)
   d. frontend → reviewer → [fix pass if REVISE, max 1 iteration]
   e. k8s-helm (when ready, no review loop)
```

Steps 3a and 3b are sequential by default (backend establishes API contracts that workflow consumes). When workflow changes are independent of backend (e.g., modifying an existing flow's retry policy), they may run concurrently. Steps 3c and 3d may also be concurrent when frontend does not depend on pending backend changes.

The main agent orchestrates by passing context between agents: the approved plan feeds into generators, generator completion reports feed into the reviewer, reviewer findings feed back to generators for fix passes. If issues persist after one fix pass, they are escalated to the user.

See `CLAUDE.md` §Implementation Workflow for the authoritative reference.

### Plan quality checklist

A good implementation plan produced during the Plan phase should cover:

1. **Scope and goals** — What the feature does (1-3 sentences), which user groups it serves (DE, DA, DG, common), what success looks like.
2. **Files to create or modify** — For each file: exact path (following existing conventions), purpose (one line), key contents (classes, functions, endpoints — names only, not implementations).
3. **Component boundaries** — Which agent owns which files (backend, workflow, frontend). Data flow between components (API contracts, Kestra flow inputs/outputs). Scope boundaries — what each agent should defer to others.
4. **Acceptance criteria** — Concrete, testable conditions per component: endpoints that must exist, response shapes, error cases, flows that must be deployable, pages that must render, which test categories are needed.
5. **Implementation sequence** — Recommended order of agent invocations with dependencies and concurrency opportunities noted.

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

Prauto is the autonomous PR worker -- a cron-driven system that picks up GitHub issues labeled `prauto:ready`, produces implementation PRs via Claude Code CLI, and manages the full issue-to-PR lifecycle. It lives in `.prauto/` (config, shell libraries, prompt templates, runtime state). See `spec/AI_PRAUTO.md` for the full specification (lifecycle labels, heartbeat cycle, phase state machine, code review, squash-finalize).

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
4. **Implement features** using the plan → approve → generate → evaluate workflow: Plan mode → approve → `backend` → `reviewer` → `test` → `frontend` → `reviewer` → `k8s-helm`

Steps 1-2 ensure every spec follows MANIFESTO conventions.

---

## Design Principles

1. **Context before code** — The agent reads the spec hierarchy (MANIFESTO → ARCHITECTURE → feature specs) before generating implementation. `CLAUDE.md` is the entry point that orients the agent.

2. **Spec as the source of truth** — All naming and user-group taxonomy derive from `MANIFESTO_en.md`. The `plan-doc` skill routes new documents to the correct tier automatically.

3. **User-group-driven organization** — Features, API routes, and UI entry points are organized by user group (DE, DA, DG), mirroring the MANIFESTO's structure.

4. **API-first development** — The `backend` subagent implements API routes as the single source of truth for the API contract, following the three-tier URI pattern defined in feature specs. FastAPI auto-generates OpenAPI documentation from the implementation.

5. **Least privilege** — Agents read and inspect freely but cannot change shared state without user confirmation. Destructive cluster operations are blocked.

6. **Self-verifying subagents** — `backend`, `workflow`, `frontend`, and `test` agents have Bash access to run tests and type-checks, catching errors before reporting completion. Self-verification is necessary but not sufficient — it is complemented by independent review (see principle 8).

7. **Context confinement** — Each subagent operates in a fresh, focused context containing only the approved plan, relevant spec, and files in its scope. This keeps the main conversation clean for orchestration and prevents implementation noise from degrading decision quality. Delegate implementation to generator agents rather than writing code in the main conversation.

8. **Generator-evaluator separation** — Generators (backend, workflow, frontend) write code and self-test. An independent `reviewer` agent evaluates the output against the spec and approved plan. Self-evaluation is insufficient for quality assurance — models tend to praise their own work. External critique from a separate context is a stronger signal. (Source: [Anthropic harness design research](https://www.anthropic.com/engineering/harness-design-long-running-apps).)

9. **Model-appropriate roles** — Use the strongest model (opus) for the `reviewer` role, which requires judgment, reasoning, and resistance to self-praise patterns. Use faster models (sonnet) for volume code generation. Planning uses Claude's built-in Plan mode (which runs on the session's current model) rather than a dedicated subagent.

10. **Bounded iteration** — Review loops are capped at 1 fix iteration per generator to control cost and latency. Unresolved issues after one fix pass are escalated to the user rather than looping indefinitely.
