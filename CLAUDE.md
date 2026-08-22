# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

@AGENTS.md

The import above carries the full repository purpose, shell/deployment commands, key design
decisions, spec convention, git commit convention, the plan → approve → generate → evaluate
implementation workflow, and the integration test protocol — it is auto-loaded into context by
Claude Code the same as this file. Everything below adds only what's specific to running that
workflow under Claude Code; see also `scaffold/README.md` for how the same workflow runs under
other coding-agent CLIs (e.g. Codex).

## Implementation Workflow — Claude Code binding

`AGENTS.md §Implementation Workflow` describes the plan → approve → generate → evaluate steps in
CLI-agnostic terms. Under Claude Code specifically:

- **You MUST enter Plan mode** before writing any implementation code unless all of
  `AGENTS.md`'s skip-plan criteria are met. Never self-classify a task as "trivial" to skip
  planning.
- Each role named in `AGENTS.md`'s steps 4–9 (`spec`, `backend`, `airflow-dag`, `test`,
  `frontend`, `k8s-helm`, and evaluators `reviewer`/`spec-reviewer`/`test-reviewer`/
  `security-reviewer`) maps to a `.claude/agents/<name>.md` subagent — invoke it with the
  `Agent` tool. Each subagent file is a thin binding (Claude-Code tool/model/hook/memory
  frontmatter) that points to the canonical role definition in `scaffold/roles/<name>.md`.
- **Orchestration**: drive steps 4–9 with direct `Agent` calls by default — spawn each generator,
  then its reviewer(s), following the per-stage loop and delegation rules in `AGENTS.md`. When
  workflow-based multi-agent execution is desired, write a dynamic workflow script (`Workflow`
  tool): compose an agent fleet from the subagent definitions in `.claude/agents/`, pairing each
  generator with an adversarial reviewer, and shape the fan-out/parallelism to the task. The
  per-stage loop of `AGENTS.md` steps 4–9 is one of the simplest examples of such a workflow,
  checked in as `.claude/workflows/wf-minimal.js` (`args = {plan, stages, security}`) — also
  runnable as `/wf-minimal`. Plan approval is standing authorization to invoke the `Workflow`
  tool when the plan opts in; the per-run launch prompt remains the user's control point.
- For standalone spec authoring outside an implementation run, use `/spec-write` directly; inside
  a run the `spec` subagent (step 4) covers it.

## Testing prauto

Due to Claude's nested-run limit, testing `.prauto/heartbeat.sh` from inside a Claude Code session requires unsetting the `CLAUDECODE` env var:

```bash
env -u CLAUDECODE bash -x .prauto/heartbeat.sh
```

## Claude Code Configuration

**Skills**: `k8s-work`, `spec-write`, `datahub-api`, `prauto-check-status`, `prauto-run-heartbeat`, `k8s-deploy`, `ref-setup`, `spec-sync-with-impl`, `spec-harmonize`, `spec-reduce`, `spec-to-bulk-issue`, `test-manual-api-wired`, `test-manual-ui` (browser-driven sibling — manual UC walkthrough with UI+backend dual confirmation)
_(Note: `datahub-api` requires `ref/github/datahub/` — run `/ref-setup` once if not present.)_
**Subagents**: `reviewer` (evaluator, opus), `test-reviewer` (evaluator, opus), `security-reviewer` (evaluator, opus), `spec-reviewer` (evaluator, opus), `backend`, `airflow-dag`, `test`, `frontend`, `k8s-helm`, `spec` (sonnet generators). Each subagent's body is a pointer to its canonical definition in `scaffold/roles/<name>.md` — edit the role's content there, not in `.claude/agents/`. Evaluators keep persistent cross-session memory in `.claude/agent-memory/<name>/` (checked in); non-Claude-Code backends use a separate, independently-accumulated store at `scaffold/memory/<name>/`.
**Workflows**: dynamic workflow scripts written per task — agent fleets composed from the `.claude/agents/` sub-agent definitions with adversarial generator → reviewer pairing. `.claude/workflows/wf-minimal.js` is the simplest checked-in example (`AGENTS.md` steps 4–9; also runnable as `/wf-minimal`); the default driver remains direct Agent calls. `scaffold/bin/run-workflow.sh` is the equivalent driver for coding-agent CLIs without a native workflow primitive.
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
**Hooks**: `.claude/hooks/` — integration-test preflight (blocking), plan-gate reminder, permission-hygiene warning, commit confirmation. Wired via `.claude/settings.json`; tool-event hooks are gated by settings-level `if` filters with in-script guards as backup. Generator agents additionally run per-agent hooks (ruff on edited Python files; frontend typecheck on Stop), wired in their frontmatter. The lint, typecheck, and integration-test-preflight guarantees also hold at the test-tooling layer (`scaffold/bin/lint-python.sh`, `src/frontend/package.json`'s `pretest` script, `tests/integration/conftest.py`'s `require_server` fixture) so they apply under any coding-agent CLI or a human running the same commands; the plan-gate policy itself is stated as plain convention in `AGENTS.md §Implementation Workflow`, which any CLI reads. Three conveniences — the commit-message-format check, `permission-hygiene-check.sh`, and the statusline (not a hook, listed separately below) — are Claude-Code-only with no portable equivalent; see `scaffold/README.md §What deliberately isn't here` for the rationale.
**Statusline**: `.claude/statusline.sh` — model · effort · context-usage · cwd · git-branch · 5-hour usage window. Context and reset segments read native stdin fields (`context_window.*`, `rate_limits.five_hour.*`, Claude Code ≥ 2.1.132); the rate-limit segment needs a Claude.ai Pro/Max session and is omitted silently otherwise. Claude-Code-only — no equivalent is planned for other CLIs (each has its own status UI).
**End-user plugin**: `plugin/` is the shippable End-User AI Scaffold (distinct from this developer `.claude/` scaffold) — public-API-only skills for consumers of a deployed DataSpoke; repo root `.claude-plugin/marketplace.json` makes the repo a single-plugin marketplace. See `spec/AI_PLUGIN.md`. Claude-Code-specific packaging; not yet ported to other CLIs.
