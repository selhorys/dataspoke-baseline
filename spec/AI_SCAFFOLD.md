# DataSpoke AI Coding Scaffold

## Table of Contents

1. [Purpose](#purpose)
2. [Scaffold Structure](#scaffold-structure)
3. [Skills](#skills)
4. [Roles](#roles)
5. [Codex Binding](#codex-binding)
6. [Permissions](#permissions)
7. [Prauto](#prauto)
8. [Building a Custom Spoke](#building-a-custom-spoke)
9. [Design Principles](#design-principles)

---

## Purpose

The DataSpoke project develops two core artifacts (from `spec/MANIFESTO_en.md` §2):

1. **Baseline Product** — a foundational data catalog implementation of the five MANIFESTO
   features (Ingestion Control, Validation, Ontology Generation, Metadata Generation,
   Governance).
2. **Productized Scaffold** — a framework for custom development, comprising specs, a
   deployment subsystem (`helm-charts/`), and coding-agent utilities.

This document covers the **Developer AI Scaffold** — the MANIFESTO §2.2 "AI Scaffold" that makes
AI-assisted *development of the product* immediately productive from the first session. It is
complemented by a distinct, sibling deliverable: the **End-User AI Scaffold** — a distributable
Claude Code plugin that helps engineers *consume a running DataSpoke* through its public API,
specified in `spec/AI_PLUGIN.md`. The two never overlap in access: the Developer scaffold has
full repo access (specs, `src/`, helm, DB); the End-User plugin sees only the public API surface
of a deployed instance.

A well-structured scaffold removes the bootstrapping cost of AI coding: the agent knows the
project layout, naming conventions, spec hierarchy, and operational environment before writing a
single line of code. The Development Scaffold (Kubernetes-based deployment subsystem covering
both dev and prod profiles) is specified separately in `spec/feature/HELM_CHART.md`.

The Developer scaffold is split into three layers so the same plan → approve → generate →
evaluate workflow runs under more than one coding-agent CLI, not only Claude Code:

- an **agent-agnostic core** (`scaffold/`) that is the single source of truth for what each role
  knows and does;
- a **Claude Code binding** (`.claude/`) — the fullest-featured binding, with native subagents,
  hooks, a workflow DSL, skills, permissions, and a statusline;
- a **Codex binding** — root `AGENTS.md` (read natively by Codex) plus `scaffold/bin/` scripts
  standing in for the native subagent/workflow primitives Codex lacks.

Every role, and the plan → approve → generate → evaluate workflow itself, is defined once and
read by whichever binding is active for the session.

---

## Scaffold Structure

### `scaffold/` — agent-agnostic core

```
scaffold/
├── roles/    # canonical role definitions — one .md per generator/evaluator
├── memory/   # persistent cross-session lessons for evaluator roles (non-Claude-Code backends)
├── bin/      # scripts that drive a role or a full stage loop via a chosen backend CLI
└── README.md
```

`scaffold/roles/<name>.md` is the canonical definition of each role (reading list, source layout,
conventions, invocation modes, and — for evaluators — the scoring rubric and verdict format).
`scaffold/bin/run-stage.sh` and `run-workflow.sh` let a CLI with no native subagent/workflow
primitive drive one role, or a full generate → evaluate stage loop, via `--agent {claude|codex}`.
`scaffold/bin/lint-python.sh` is the portable form of the ruff-check hook. See `scaffold/README.md`
for the full description of each piece; it is not restated here.

### `.claude/` — Claude Code binding

`.claude/` contains: `skills/` (prompt extensions — one directory per skill), `agents/` (thin
per-role bindings — frontmatter plus short Claude-Code binding notes, each pointing at its
`scaffold/roles/<name>.md`), `agent-memory/` (evaluator cross-session memory, checked in), `hooks/`
(shell scripts invoked by Claude Code events — `settings.json`-wired lifecycle hooks:
integration-test preflight, plan-gate reminder, permission-hygiene warning, commit confirmation;
plus per-agent hooks wired in agent frontmatter: ruff lint on edited Python files, frontend
typecheck on Stop), `workflows/` (dynamic agent-fleet scripts, e.g. `wf-minimal.js`),
`statusline.sh` (status line composer), `settings.json` (tool permissions + hooks + statusLine),
and `settings.local.json` (local overrides). See §Skills and §Roles below for the full
catalogue.

### Other structural elements

| Element | Role |
|---------|------|
| `AGENTS.md` | Agent-agnostic root instructions read natively by Codex and other CLIs: project context, spec hierarchy, implementation workflow in CLI-neutral terms |
| `CLAUDE.md` | Claude-Code-specific binding on top of `AGENTS.md`: Plan mode, `Agent`/`Workflow` tool usage, skills/subagents/hooks/permissions/statusline inventory |
| `spec/` | Hierarchical spec documents (MANIFESTO → ARCHITECTURE → feature specs) |
| `helm-charts/` | Umbrella Helm chart + `bin/` install/uninstall/build scripts + dev peripherals. See `spec/feature/HELM_CHART.md` |
| `ref/` | External source code for AI reference (DataHub v1.6.0, downloaded via `/ref-setup`) |
| `.prauto/` | Autonomous PR worker: cron-driven issue-to-PR automation, Claude-Code-CLI-only. See `spec/AI_PRAUTO.md` |

---

## Skills

Skills are prompt extensions that give the agent specialized context for a specific domain. They
are a Claude Code mechanism: they live in `.claude/skills/<name>/SKILL.md` and are loaded when
invoked explicitly (`/skill-name`) or when Claude detects a matching context.

| Skill | Purpose |
|-------|---------|
| `k8s-work` | Kubernetes cluster management: one-time health checks, continuous monitoring with polling during installs, and kubectl/helm operations. Runs as a forked subagent; reads cluster config from `helm-charts/.env.dev` (dev) or `helm-charts/.env.prod` (prod) |
| `spec-write` | Author timeless specification documents in `spec/` (top-level or `spec/feature/<FEATURE>.md`) following the project hierarchy, naming conventions, and templates. Not for implementation plans |
| `datahub-api` | Reference and coding guide for DataHub integration in backend development. Covers entities, aspects, lineage, URNs, ingestion/emission, GraphQL, REST, and the `acryl-datahub` SDK. Requires `/ref-setup` first |
| `prauto-check-status` | Status dashboard across all prauto lifecycle labels; predicts what the next heartbeat will do |
| `prauto-run-heartbeat` | Monitored test-run of `.prauto/heartbeat.sh`; watches state files, reads logs, diagnoses + fixes script errors across up to 3 retry cycles |
| `k8s-deploy` | Deployment management for both dev and prod profiles: configure, install (full or partial), reinstall (selective component reset with PVC + DB cleanup), uninstall (full or partial), health-check, and run-api (rebuild + redeploy the in-cluster API via `--components api`). Drives `./helm-charts/bin/install.sh --profile {dev\|prod}` and the related scripts. HTTP services are accessed via nginx-ingress; in shared ingress mode TCP services are reached on `127.0.0.1` via `bin/port-forward.sh`. Accepts action + optional component/options as arguments |
| `ref-setup` | Download AI reference materials (external source code for AI assistant reference) with interactive selection; monitor in background until complete |
| `spec-sync-with-impl` | Bidirectional spec ↔ impl sync. Accepts preset scopes (prauto, ai-scaffold, k8s-deploy, helm-charts, api, ref, backend, frontend) or a free-form description of any area; resolves a candidate file list with the user, audits gaps, asks how to resolve each gap (spec→impl, impl→spec, or leave-as-flagged), then applies the chosen edits |
| `spec-harmonize` | Propagate spec changes to sibling/parent specs and harness docs. When a spec is created, modified, or deleted, updates all documents that reference or list it |
| `spec-reduce` | Audit and trim bloated specs, scaffold docs, and READMEs. Removes implementation details, eliminates cross-tier duplication, enforces abstraction-level discipline |
| `spec-to-bulk-issue` | Analyze specs to find unimplemented components, write ordered issue tickets in `issues/`, revise existing issues, and optionally register them to GitHub with `prauto:ready` label |
| `test-manual-api-wired` | Guided manual harness for a single `tests/integration/api_wired/` UC scenario: reads the test file, prints each REST request, pauses for approval before mutations, fires the call, prints the response, and probes side effects (DB rows, DataHub aspects, K8s Secrets) |
| `test-manual-ui` | Browser-driven sibling of `test-manual-api-wired`: walks the same UC scenario through the reference UI, scripting each gesture from the test file, then confirms both the observed UI state and the backend side effect (REST read-back + probes). Human-in-the-loop stand-in for the unbuilt automated E2E layer |

Each skill's SKILL.md is the authoritative reference for its behavior, invocation options, and
allowed tools. Skills are Claude-Code-only; a non-Claude-Code session has no equivalent and works
from `AGENTS.md` and the spec hierarchy directly.

---

## Roles

Every generator and evaluator role is defined once in `scaffold/roles/<name>.md` — reading list,
source layout, conventions, invocation modes, and (for evaluators) the scoring rubric and verdict
format. That file is canonical regardless of which coding-agent CLI is driving a session.

`.claude/agents/<name>.md` is the Claude Code binding of a role: Claude-Code-specific frontmatter
(`tools:`, `model:`, `hooks:`, `memory:`, `skills:`) plus a short pointer to the matching
`scaffold/roles/<name>.md` body. As Claude Code subagents, they fall into two kinds
following the **generator → evaluator** pattern (see §Design Principles). Planning is handled by
Claude's built-in Plan mode before generators are invoked.

**Why delegate to roles**: The primary reason to delegate implementation to a generator role is
**context confinement**. Each generator operates in a fresh, focused context — it sees only the
approved plan, the relevant spec, and the files in its scope. This prevents the main conversation
from accumulating implementation noise (hundreds of lines of generated code, test output, linter
errors) that degrades the quality of subsequent decisions. The main agent stays clean for
orchestration: passing plans, routing reviewer findings, and deciding what to run next. For
non-trivial implementation, delegate to the appropriate generator agent rather than writing code
directly in the main conversation.

### Evaluator (opus model)

| Role | Scope | Tools |
|------|-------|-------|
| `reviewer` | Independently reviews code-generator output against spec + implementation plan. Produces structured pass/fail scoring across 5 criteria (spec compliance, architecture adherence, code quality, completeness, inter-component consistency). Invoked after `backend`, `airflow-dag`, and `frontend` generators | Read, Glob, Grep, Bash |
| `test-reviewer` | Independently reviews test-generator output (pytest **and** Playwright E2E). Produces structured pass/fail scoring across 5 test-specific criteria: spec traceability, spec-derived (vs impl-calibrated) assertions, failure-mode coverage, plausibly-broken-impl sensitivity, and property-based testing opportunity (advisory). Invoked after the `test` generator | Read, Glob, Grep, Bash |
| `spec-reviewer` | Independently reviews spec-generator output against the spec hierarchy + plan. Produces structured pass/fail scoring across 5 spec-specific criteria: hierarchy/priority compliance, internal consistency & naming, timeless & no-bloat, completeness vs plan, altitude. Invoked after the `spec` generator | Read, Glob, Grep |
| `security-reviewer` | Parallel security review when a generator's diff touches sensitive paths (all of `src/shared/**` and `src/backend/**`, `src/api/` auth / middleware / routers, migrations, Helm credentials, new dependencies, `.prauto/`). Scores injection, authn/authz, secrets, input validation, supply chain, DataHub emission, crypto. Authoritative glob list lives in the role file | Read, Glob, Grep, Bash |

All four reviewers use read-only tools — they analyze and report but do not write code.

### Generators (sonnet)

| Role | Scope | Tools |
|------|-------|-------|
| `spec` | Specification documents under `spec/` (top-level + `feature/<FEATURE>.md`). Authors and harmonizes timeless reference specs via the `spec-write`/`spec-harmonize`/`spec-sync-with-impl` skills. Leads the run so code generators read the updated spec; runs only when the plan adds or changes specs. Supports fix pass mode | Read, Write, Edit, Glob, Grep |
| `backend` | FastAPI routes, services, shared libs in `src/api/`, `src/backend/`, `src/shared/`. Reads feature specs and the approved plan. Self-verifies with `pytest`. Supports fix pass mode for reviewer findings | Read, Write, Edit, Glob, Grep, Bash |
| `airflow-dag` | Airflow DAG Python files in `src/workflows/dags/` and workflow parameter modules. Orchestrates `src/backend/` services via HttpOperator tasks. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `test` | Tests across all layers in `tests/`: Python unit / spot / api-wired (pytest) **and** Playwright/TypeScript E2E in `tests/e2e/` (use-case + ground groups). Follows `spec/TESTING.md`. Supports reviewer-directed testing mode to verify specific findings | Read, Write, Edit, Glob, Grep, Bash |
| `frontend` | Next.js/TypeScript code in `src/frontend/`. Reads `FRONTEND_*.md` specs. Self-verifies with `npm test` and `tsc`. Supports fix pass mode | Read, Write, Edit, Glob, Grep, Bash |
| `k8s-helm` | Helm charts, Dockerfiles, Kubernetes manifests, dev environment scripts. No review loop (infrastructure changes are lower-risk) | Read, Write, Edit, Glob, Grep, Bash |

### Implementation workflow

`AGENTS.md §Implementation Workflow` is the authoritative, CLI-agnostic reference for the
plan → approve → generate → evaluate steps (skip-plan criteria, the numbered step 1–9 sequence,
concurrency opportunities, the security-reviewer pairing rule, and the "generator ≠ reviewer,
one fix pass, escalate on persistent REVISE/ESCALATE" rule). It is not restated here.

Under Claude Code specifically: Plan mode is the planning step (step 2); each named role maps to
a `.claude/agents/<name>.md` subagent invoked with the native `Agent` tool; and the generate →
evaluate cycles run either as direct `Agent` calls (default) or via a dynamic `Workflow` script —
`.claude/workflows/wf-minimal.js` is the checked-in example of the latter, covering the same
step 4–9 loop. See `CLAUDE.md §Implementation Workflow — Claude Code binding` for these specifics,
and §Codex Binding below for how a Codex session drives the identical steps.

`AGENTS.md` step 2 (the planning step) expects the plan to have a specific shape — the
§Plan quality checklist below is that shape's canonical, CLI-agnostic definition.

### Plan quality checklist

Agent-agnostic — the same plan a human approves before any generator runs, regardless of which
CLI drives the session. A good implementation plan produced during the Plan phase should cover:

1. **Scope and goals** — What the feature does (1-3 sentences), which MANIFESTO feature(s) it
   belongs to or extends (Ingestion Control, Validation, Ontology Generation, Metadata
   Generation, Governance), what success looks like.
2. **Files to create or modify** — For each file: exact path (following existing conventions),
   purpose (one line), key contents (classes, functions, endpoints — names only, not
   implementations).
3. **Component boundaries** — Which role owns which files (backend, airflow-dag, frontend). Data
   flow between components (API contracts, Airflow DAG inputs/outputs). Scope boundaries — what
   each role should defer to others.
4. **Acceptance criteria** — Concrete, testable conditions per component: endpoints that must
   exist, response shapes, error cases, flows that must be deployable, pages that must render,
   which test categories are needed.
5. **Implementation sequence** — Recommended order of role invocations with dependencies and
   concurrency opportunities noted.

---

## Codex Binding

Codex (or any coding-agent CLI with no built-in subagent/hook/workflow primitive) reads root
`AGENTS.md` automatically as its project instructions — no separate configuration step is
needed. It drives the plan → approve → generate → evaluate workflow of `AGENTS.md
§Implementation Workflow` using `scaffold/bin/`:

- `scaffold/bin/run-stage.sh <role> <plan-file> --agent codex [--input FILE] [--model NAME]`
  invokes one role once, non-interactively — the substitute for Claude Code's native `Agent`
  tool call against a `.claude/agents/<name>.md` subagent.
- `scaffold/bin/run-workflow.sh <plan-file> --agent codex [--security s1,s2] <stage> [<stage>
  ...]` drives a full generate → evaluate stage loop across an ordered list of stages (one fix
  pass on REVISE, escalate on persistent REVISE or any ESCALATE) — a bash port of
  `.claude/workflows/wf-minimal.js`'s state machine, the substitute for Claude Code's native
  `Workflow` tool.
- Evaluator roles accumulate cross-session memory in `scaffold/memory/<evaluator>/MEMORY.md`
  (read-before/append-after, per each role's `scaffold/roles/<name>.md` instructions) — a
  separate store from Claude Code's own `.claude/agent-memory/<name>/`.

The `--agent claude` path is fully implemented and needs no qualification. The `--agent codex`
path is unverified: `invoke_codex()` in `scaffold/bin/run-stage.sh` is an explicit TBD
placeholder — no `codex` CLI was available when it was written, so its flags are best-effort
pending a `codex exec --help` check, and since `run-workflow.sh` calls `run-stage.sh` per stage,
the entire `--agent codex` path inherits that gap. See `scaffold/README.md` for the full
description of the `bin/` scripts and their flags; it is not restated here.

### Not ported to Codex (or other non-Claude-Code CLIs)

Three Claude Code conveniences have no portable equivalent, by design:

| Component | Why it stays Claude-Code-only |
|-----------|-------------------------------|
| `.claude/statusline.sh` | Pure CLI chrome — reads Claude Code's native stdin fields (context window, rate limits); other CLIs have their own status UI |
| `confirm-commit.sh`'s commit-message-format check | Its rule is carried instead as a plain convention in `AGENTS.md §Git Commit Convention` rather than forcing a new git-hook framework into a repo that otherwise has none |
| `permission-hygiene-check.sh` | Specific to `.claude/settings.local.json` bloat, meaningless outside Claude Code's permission model |

The lint and typecheck guarantees behind the remaining hooks (ruff on edited Python, frontend
typecheck) hold for any caller regardless: `scaffold/bin/lint-python.sh` is the portable form of
the ruff check, and `src/frontend/package.json`'s `pretest` script runs `tsc` ahead of the test
suite. `.claude/agent-memory/` (Claude Code's own evaluator memory store) and `scaffold/memory/`
are separate stores that accumulate independently.

Prauto (`spec/AI_PRAUTO.md`) and the End-User AI Scaffold (`spec/AI_PLUGIN.md`, `plugin/`) remain
Claude-Code-CLI-only for now — generifying them to run under other coding-agent CLIs is a planned
follow-up.

---

## Permissions

Defined in `.claude/settings.json`. Claude-Code-only — Codex and other CLIs have their own
permission models. The guiding principle: **read freely, mutate with confirmation, never
destroy**.

| Category | Policy | Examples |
|----------|--------|----------|
| Read-only | Auto-allowed | `kubectl get`, `helm list`, `git log`, `docker ps` |
| Reference docs | Auto-allowed | `WebSearch`, `WebFetch` to framework/tool documentation domains |
| Skills | Auto-allowed / prompt | Most skills auto-allowed; `prauto-run-heartbeat`, `ref-setup`, and `spec-to-bulk-issue` require user confirmation (side effects) |
| Deployment scripts | Auto-allowed | `./helm-charts/bin/install.sh`, `./helm-charts/bin/uninstall.sh`, `./helm-charts/bin/health-check.sh`, `./helm-charts/bin/build-image.sh` |
| Mutating | Prompt for confirmation | `kubectl apply`, `helm install`, `helm upgrade` |
| Destructive | Always blocked | `kubectl delete namespace`, `rm -rf`, `sudo` |

The full allow/deny lists are in `.claude/settings.json`. The settings file is the authoritative
reference.

---

## Prauto

Prauto is the autonomous PR worker -- a cron-driven system that picks up GitHub issues labeled
`prauto:ready`, produces implementation PRs via the Claude Code CLI, and manages the full
issue-to-PR lifecycle. It lives in `.prauto/` (config, shell libraries, prompt templates,
runtime state) and is Claude-Code-CLI-only. See `spec/AI_PRAUTO.md` for the full specification
(lifecycle labels, heartbeat cycle, phase state machine, per-stage review, dev cluster and
deploys, squash-finalize).

---

## Building a Custom Spoke

The scaffold is designed to be forked and adapted. A custom Spoke is a DataSpoke implementation
tailored to an organization's data sources, domain vocabulary, and operational requirements.

### Typical customization points

| What to customize | Where |
|-------------------|-------|
| Features, product identity | `spec/MANIFESTO_*.md` |
| Tech stack, system components | `spec/ARCHITECTURE.md` |
| Baseline feature specs | `spec/feature/` |
| API routers and backend services | `src/api/`, `src/backend/` |
| Cluster and namespace config | `helm-charts/.env.dev` / `helm-charts/.env.prod` |
| Role definitions | `scaffold/roles/` (canonical); `.claude/agents/` for Claude-Code-specific binding tweaks |

### Recommended sequence

1. **Revise the manifesto** — adjust or add features; pick the function namespaces that
   host any organization-specific extensions
2. **Run `/spec-write`** — update architectural specs, then baseline and spoke feature specs
3. **Run `/k8s-deploy install`** — bring up the DataHub environment
4. **Implement features** using the plan → approve → generate → evaluate workflow of `AGENTS.md
   §Implementation Workflow`: Plan → approve → `spec` → `spec-reviewer` (when specs change) →
   `backend` → `reviewer` → `airflow-dag` → `reviewer` → `test` → `test-reviewer` → `frontend` →
   `reviewer` → `k8s-helm`. The sequence is CLI-agnostic; drive it with Claude Code's `Agent`
   tool or with `scaffold/bin/run-stage.sh` / `run-workflow.sh` under another CLI.

Steps 1-2 ensure every spec follows MANIFESTO conventions.

---

## Design Principles

1. **Context before code** — The agent reads the spec hierarchy (MANIFESTO → ARCHITECTURE →
   feature specs) before generating implementation. `AGENTS.md` (or its Claude Code binding,
   `CLAUDE.md`) is the entry point that orients the agent.

2. **Spec as the source of truth** — All naming and feature taxonomy derive from
   `MANIFESTO_en.md`. The `spec-write` skill routes new documents to the correct tier
   automatically.

3. **Capability-driven organization** — Features are organized by MANIFESTO capability
   (Ingestion Control, Validation, Ontology Generation, Metadata Generation, Governance).
   Each capability owns a top-level namespace under `/spoke/` — `/spoke/governance/`,
   `/spoke/ingestion/`, `/spoke/validation/`, `/spoke/ontogen/`, `/spoke/metagen/`.

4. **API-first development** — The `backend` role implements API routes as the single source
   of truth for the API contract, following the function-namespace URI pattern defined in
   feature specs. FastAPI auto-generates OpenAPI documentation from the implementation.

5. **Least privilege** — Agents read and inspect freely but cannot change shared state without
   user confirmation. Destructive cluster operations are blocked.

6. **Self-verifying generators** — `backend`, `airflow-dag`, `frontend`, and `test` roles have
   shell access to run tests and type-checks, catching errors before reporting completion.
   Self-verification is necessary but not sufficient — it is complemented by independent review
   (see principle 8).

7. **Context confinement** — Each role operates in a fresh, focused context containing only
   the approved plan, relevant spec, and files in its scope. This keeps the main conversation
   clean for orchestration and prevents implementation noise from degrading decision quality.
   Delegate implementation to generator roles rather than writing code in the main
   conversation.

8. **Generator-evaluator separation** — Generators (spec, backend, airflow-dag, frontend, test)
   produce specs, code, or tests and self-verify. An independent evaluator role — `reviewer` for
   code, `test-reviewer` for tests, `spec-reviewer` for specs — then evaluates the output against
   the spec hierarchy and approved plan.
   Self-evaluation is insufficient for quality assurance — models tend to praise their own work.
   External critique from a separate context is a stronger signal. (Source:
   [Anthropic harness design research](https://www.anthropic.com/engineering/harness-design-long-running-apps).)

9. **Model-appropriate roles** — Use the strongest available model for evaluator roles
   (`reviewer`, `test-reviewer`, `security-reviewer`, `spec-reviewer`), which require judgment,
   reasoning, and resistance to self-praise patterns; use a faster model for generators (`spec`,
   `backend`, `airflow-dag`, `test`, `frontend`, `k8s-helm`). The Claude Code binding instantiates
   this as opus for evaluators and sonnet for generators (see each role's `model:` frontmatter in
   `.claude/agents/`). Planning and orchestration use the driving CLI's own top-level session
   (Claude Code's built-in Plan mode, expected opus) rather than a dedicated subagent.

10. **Bounded iteration** — Review loops are capped at 1 fix iteration per generator to control
    cost and latency. Unresolved issues after one fix pass are escalated to the user rather than
    looping indefinitely.
