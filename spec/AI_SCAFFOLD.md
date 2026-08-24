# DataSpoke AI Coding Scaffold

## Table of Contents

1. [Purpose](#purpose)
2. [Scaffold Structure](#scaffold-structure)
3. [Skills](#skills)
4. [Roles](#roles)
5. [CLI Bindings](#cli-bindings)
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
- a **Claude Code binding** (`.claude/`) with native subagents, permissions,
  and CLI-specific presentation;
- a **Codex binding** (`.codex/`) with native subagents, skills, and sandbox policy;
- explicit validation and conformance utilities (`scaffold/bin/`) that do not execute agents.

Every role, and the plan → approve → generate → evaluate workflow itself, is defined once and
read by whichever binding is active for the session.

---

## Scaffold Structure

### `scaffold/` — agent-agnostic core

```
scaffold/
├── roles/    # canonical role definitions — one .md per generator/evaluator
├── memory/   # canonical, reviewed cross-session lessons for evaluator roles
├── contracts/# structured completion and evaluator-verdict schemas
├── bin/      # explicit validation and binding-conformance utilities
└── README.md
```

`scaffold/roles/<name>.md` is the canonical definition of each role. Generator bindings read their
role directly. Before generation, the parent captures each evaluator binding, canonical role,
relevant `scaffold/memory/<name>/` contents, and verdict contract; the evaluator session consumes
that snapshot and never reloads live authority paths. `scaffold/bin/` contains explicit validation
and conformance utilities only. See `scaffold/README.md` for details.

### `.claude/` — Claude Code binding

`.claude/` contains: `skills/` (thin views of shared skills), `agents/` (thin
per-role bindings — frontmatter plus short Claude-Code binding notes, each pointing at its
`scaffold/roles/<name>.md`), `statusline.sh` (status line composer), `settings.json` (tool
permissions + statusLine), and a Prauto-only checked-in workflow under `workflows/`,
and `settings.local.json` (local overrides). See §Skills and §Roles below for the full
catalogue.

### `.codex/` — Codex binding

`.codex/agents/*.toml` binds each canonical role to Codex-native subagents and declares its model,
reasoning, and sandbox. Generator roles use workspace-write access; evaluator roles are explicitly
read-only. `.codex/config.toml` carries project-scoped concurrency and defaults. Paths are
repository-relative and resolved from the repository root.

### Other structural elements

| Element | Role |
|---------|------|
| `AGENTS.md` | Agent-agnostic root instructions read natively by Codex and other CLIs: project context, spec hierarchy, implementation workflow in CLI-neutral terms |
| `CLAUDE.md` | Claude-Code-specific binding on top of `AGENTS.md`: Plan mode, direct native-agent orchestration, skills/subagents/permissions/statusline inventory |
| `.agents/skills/` | Canonical shared skills; CLI-specific skill directories are bindings or generated mirrors rather than independent copies |
| `spec/` | Hierarchical spec documents (MANIFESTO → ARCHITECTURE → feature specs) |
| `helm-charts/` | Umbrella Helm chart + `bin/` install/uninstall/build scripts + dev peripherals. See `spec/feature/HELM_CHART.md` |
| `ref/` | External source code for AI reference (DataHub v1.6.0, downloaded via `/ref-setup`) |
| `.prauto/` | Autonomous PR worker: scheduled issue-to-PR automation (loop master + agent-agnostic contract; Claude Code / Codex). See `spec/AI_PRAUTO.md` |

---

## Skills

Skills are prompt extensions that give the agent specialized context for a specific domain. The
project-owned canonical copies live in `.agents/skills/<name>/SKILL.md`. Each CLI discovers that
tree directly or exposes a thin/generated binding; behavioral content is not independently
maintained in vendor directories.

| Skill | Purpose |
|-------|---------|
| `k8s-work` | Kubernetes cluster management: one-time health checks, continuous monitoring with polling during installs, and kubectl/helm operations. Runs as a forked subagent; reads cluster config from `helm-charts/.env.dev` (dev) or `helm-charts/.env.prod` (prod) |
| `spec-write` | Author timeless specification documents in `spec/` (top-level or `spec/feature/<FEATURE>.md`) following the project hierarchy, naming conventions, and templates. Not for implementation plans |
| `datahub-api` | Reference and coding guide for DataHub integration in backend development. Covers entities, aspects, lineage, URNs, ingestion/emission, GraphQL, REST, and the `acryl-datahub` SDK. Requires `/ref-setup` first |
| `k8s-deploy` | Deployment management for both dev and prod profiles: configure, install (full or partial), reinstall (selective component reset with PVC + DB cleanup), uninstall (full or partial), health-check, and run-api (rebuild + redeploy the in-cluster API via `--components api`). Drives `./helm-charts/bin/install.sh --profile {dev\|prod}` and the related scripts. HTTP services are accessed via nginx-ingress; in shared ingress mode TCP services are reached on `127.0.0.1` via `bin/port-forward.sh`. Accepts action + optional component/options as arguments |
| `ref-setup` | Download AI reference materials (external source code for AI assistant reference) with interactive selection; monitor in background until complete |
| `spec-sync-with-impl` | Bidirectional spec ↔ impl sync. Accepts preset scopes — infra (prauto, ai-scaffold, k8s-deploy, helm-charts, plugin) and per-domain (ingestion, validation, ontogen, metagen, governance, auth, dataset, datahub, admin, secrets, events, airflow, frontend-shared) — or a free-form description of any area; resolves a candidate file list with the user, audits gaps, asks how to resolve each gap (spec→impl, impl→spec, or leave-as-flagged), then applies the chosen edits |
| `spec-harmonize` | Propagate spec changes to sibling/parent specs and harness docs. When a spec is created, modified, or deleted, updates all documents that reference or list it |
| `spec-reduce` | Audit and trim bloated specs, scaffold docs, and READMEs. Removes implementation details, eliminates cross-tier duplication, enforces abstraction-level discipline |
| `spec-to-bulk-issue` | Analyze specs to find unimplemented components, write ordered issue tickets in `issues/`, revise existing issues, and optionally register them to GitHub with `prauto:ready` label |
| `test-manual-api-wired` | Guided manual harness for a single `tests/integration/api_wired/` UC scenario: reads the test file, prints each REST request, pauses for approval before mutations, fires the call, prints the response, and probes side effects (DB rows, DataHub aspects, K8s Secrets) |
| `test-manual-ui` | Browser-driven sibling of `test-manual-api-wired`: walks the same UC scenario through the reference UI, scripting each gesture from the test file, then confirms both the observed UI state and the backend side effect (REST read-back + probes). Human-in-the-loop stand-in for the unbuilt automated E2E layer |

Each canonical `SKILL.md` is authoritative for its behavior, invocation options, and allowed
tools. Both Claude Code and Codex may invoke skills explicitly or by context when supported.

---

## Roles

Every generator and evaluator role is defined once in `scaffold/roles/<name>.md` — reading list,
source layout, conventions, invocation modes, and (for evaluators) the scoring rubric and verdict
format. That file is canonical regardless of which coding-agent CLI is driving a session.

`.claude/agents/<name>.md` and `.codex/agents/<name>.toml` are thin native bindings of those
roles. They contain only CLI-specific model, tool, and sandbox mechanics plus a pointer to
the canonical role. Both bindings preserve the **generator → evaluator** pattern (see §Design
Principles). Planning remains in the driving session before generators are invoked.

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
| `reviewer` | Independently reviews code-generator output against spec + implementation plan. Produces structured pass/fail scoring across 5 criteria (spec compliance, architecture adherence, code quality, completeness, inter-component consistency). Invoked after `backend`, `airflow-dag`, and `frontend` generators | Read, Glob, Grep |
| `test-reviewer` | Independently reviews test-generator output (pytest **and** Playwright E2E). Produces structured pass/fail scoring across 5 test-specific criteria: spec traceability, spec-derived (vs impl-calibrated) assertions, failure-mode coverage, plausibly-broken-impl sensitivity, and property-based testing opportunity (advisory). Invoked after the `test` generator | Read, Glob, Grep |
| `spec-reviewer` | Independently reviews spec-generator output against the spec hierarchy + plan. Produces structured pass/fail scoring across 5 spec-specific criteria: hierarchy/priority compliance, internal consistency & naming, timeless & no-bloat, completeness vs plan, altitude. Invoked after the `spec` generator | Read, Glob, Grep |
| `security-reviewer` | Parallel security review when a generator's diff touches sensitive application, deployment, dependency, automation, or agent-control paths, including `.codex/`, shared skills, contracts, and evaluator memory. Scores injection, authn/authz, secrets, input validation, supply chain, DataHub emission, and crypto. Authoritative glob list lives in the role file | Read, Glob, Grep |

All four reviewers are technically read-only — Claude restricts their tools and Codex declares
`sandbox_mode = "read-only"`. A trusted orchestrator captures status and diff evidence before
invocation and injects it as data; evaluators do not execute shell commands or project scripts.

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
plan → approve → generate → evaluate steps. The native parent session coordinates every stage,
with generator and evaluator in separate native contexts and one fix pass before escalation.

Before any generator runs, the parent reads and captures the evaluator bindings, canonical reviewer
roles, verdict schema/contracts, and relevant evaluator memory from trusted repository state,
including their identities, then loads evaluator sessions from that immutable snapshot. Generated
changes to those files cannot alter the active reviewers. A client that cannot preserve this
authority fails closed and escalates before generation.

After every generator and fix pass, the parent captures complete repository evidence: status,
staged and unstaged diffs, untracked inventory and relevant contents, diff-check results, and
actual changed paths. The session-loaded evaluator receives `Pinned evaluator authority` and a
separate `Untrusted per-pass evidence` section and never reloads live authority paths. Actual paths
determine mandatory security review; a manual flag only force-enables it. The parent
validates every evaluator result against the pinned shared schema and semantic invariants.
Missing, malformed, or contradictory output is ESCALATE.

Under Claude Code, Plan mode is the planning step and each role maps to a
`.claude/agents/<name>.md` subagent invoked directly by the parent with the native `Agent` tool.
Codex uses the equivalent native project agents. Checked-in workflow scripts are not an
authoritative path for interactive development; Prauto's separate workflow remains governed by
`spec/AI_PRAUTO.md`.

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

## CLI Bindings

In an interactive developer session, Claude Code and Codex both read root `AGENTS.md`, then their
parent session uses native role bindings to drive the approved plan under the pinned-authority,
complete-evidence, and verdict-validation contract above. Native parent coordination is the sole
generator and evaluator execution path. A CLI that cannot preserve the pre-generation authority
or validate native evaluator output fails closed and escalates.

The parent captures shared evaluator lessons from `scaffold/memory/<evaluator>/` before generation
and supplies them through pinned authority. Updates to that memory are ordinary reviewed repository
changes. Vendor-private memory, if enabled, is non-authoritative and must not create a second
project history.

### Binding-specific facilities

The scaffold does not install automatic project lifecycle hooks. Plan approval, commit policy,
and generator/evaluator separation are explicit rules in `AGENTS.md`. Validation is invoked
explicitly through repository scripts and test commands; integration health is also enforced by
the pytest session fixture, and frontend tests run typechecking through the package `pretest`
script. Native client permission and sandbox controls enforce mutation boundaries. Status displays
remain CLI-local presentation.

Prauto (`spec/AI_PRAUTO.md`) is outside the interactive native-agent security model and retains
its existing Claude workflow and security boundary. A Prauto workflow verdict does not establish
approval for an interactive Developer AI Scaffold run. The End-User AI Scaffold
(`spec/AI_PLUGIN.md`, `plugin/`) is a separate Claude Code integration with its own privilege boundary.

---

## Permissions

Each binding expresses permissions in its native configuration. The guiding principle is:
**read freely, mutate within an approved generator scope, never destroy**. Evaluators are always
read-only; generators receive workspace-write only for their declared scope.

| Category | Policy | Examples |
|----------|--------|----------|
| Read-only | Auto-allowed | `kubectl get`, `helm list`, `git log`, `docker ps` |
| Reference docs | Auto-allowed | `WebSearch`, `WebFetch` to framework/tool documentation domains |
| Skills | Auto-allowed / prompt | Most skills auto-allowed; `ref-setup` and `spec-to-bulk-issue` require user confirmation (side effects) |
| Deployment scripts | Auto-allowed | `./helm-charts/bin/install.sh`, `./helm-charts/bin/uninstall.sh`, `./helm-charts/bin/health-check.sh`, `./helm-charts/bin/build-image.sh` |
| Mutating | Prompt for confirmation | `kubectl apply`, `helm install`, `helm upgrade` |
| Destructive | Always blocked | `kubectl delete namespace`, `rm -rf`, `sudo` |

The full allow/deny lists are in `.claude/settings.json`. The settings file is the authoritative
reference.

---

## Prauto

Prauto is the autonomous PR worker -- a scheduled system that picks up GitHub issues labeled
`prauto:ready`, produces implementation PRs via a headless coding-agent CLI (Claude Code or Codex,
selected by the loop master), and manages the full issue-to-PR lifecycle. It is specified as a
**loop master + contract** split: the contract (labels, phase state machine, the evidence-based
plan gate, generator ≠ reviewer, deploy ordering, the security model) lives in
`spec/AI_PRAUTO.md` and is agent-agnostic; the loop (scheduling, agent selection, subagent
dispatch) is a loop master with a reference Hermes-Agent binding. The v0.7 bash harness has been
removed; the loop master is the sole executor. See `spec/AI_PRAUTO.md` for the full specification.

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
| Role definitions | `scaffold/roles/` (canonical); `.claude/agents/` and `.codex/agents/` for CLI-specific binding mechanics |

### Recommended sequence

1. **Revise the manifesto** — adjust or add features; pick the function namespaces that
   host any organization-specific extensions
2. **Run `/spec-write`** — update architectural specs, then baseline and spoke feature specs
3. **Run `/k8s-deploy install`** — bring up the DataHub environment
4. **Implement features** using the plan → approve → generate → evaluate workflow of `AGENTS.md
   §Implementation Workflow`: Plan → approve → `spec` → `spec-reviewer` (when specs change) →
   `backend` → `reviewer` → `airflow-dag` → `reviewer` → `test` → `test-reviewer` → `frontend` →
   `reviewer` → `k8s-helm`. The sequence is driven only by the native Claude Code or Codex parent
   session; repository scripts provide validation and conformance checks but do not execute agents.

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
   `backend`, `airflow-dag`, `test`, `frontend`, `k8s-helm`). Each binding records that choice in
   its native agent configuration. Planning and orchestration use the driving CLI's top-level
   session rather than a dedicated subagent.

10. **Portable core, thin bindings** — Role behavior, reusable skills, evaluator contracts, and
    reviewer memory have one canonical project-owned source. Vendor
    directories contain only invocation and permission mechanics.

11. **Bounded iteration** — Review loops are capped at 1 fix iteration per generator to control
    cost and latency. Unresolved issues after one fix pass are escalated to the user rather than
    looping indefinitely.
