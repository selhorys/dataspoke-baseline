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
  `Agent` tool. Generator bindings read their canonical roles normally; evaluator bindings consume
  only parent-supplied pre-generation authority and per-pass evidence.
- **Orchestration**: before any generator, read and capture every required evaluator binding,
  canonical reviewer role, verdict schema/semantic contract, and relevant evaluator memory,
  including their identities. Load evaluator sessions from that snapshot. After each generator or
  fix pass, capture complete status, staged/unstaged diff, untracked contents, diff-check, and
  changed-path evidence. Invoke the session-loaded evaluator with `Pinned evaluator authority`
  containing the snapshot and a separate `Untrusted per-pass evidence` section. Never reload live
  post-generation authority paths. Validate against the pinned contract; missing authority,
  evidence, or valid output escalates. Prauto retains its separate workflow and security contract.
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
**Subagents**: `reviewer`, `test-reviewer`, `security-reviewer`, and `spec-reviewer` are read-only
evaluators; the remaining role agents are generators. Each binding points to
`scaffold/roles/<name>.md`. Shared evaluator memory lives in `scaffold/memory/<name>/`, with
`.claude/agent-memory` as a compatibility link; evaluators never write memory during review.
**Agent execution**: only the interactive parent invokes native generator and evaluator agents.
Repository shell utilities validate bindings and outputs but do not execute agents.
**Permissions**: Read-only ops auto-allowed; mutating ops prompt; destructive ops blocked. See `.claude/settings.json`.
**Validation**: no automatic project lifecycle hooks are installed. Run the repository's explicit
validation scripts and test commands; integration pytest fixtures and the frontend package
`pretest` provide suite-level enforcement. `.claude/settings.json` supplies native permissions.
**Statusline**: `.claude/statusline.sh` — model · effort · context-usage · cwd · git-branch · 5-hour usage window. Context and reset segments read native stdin fields (`context_window.*`, `rate_limits.five_hour.*`, Claude Code ≥ 2.1.132); the rate-limit segment needs a Claude.ai Pro/Max session and is omitted silently otherwise. Claude-Code-only — no equivalent is planned for other CLIs (each has its own status UI).
**End-user plugin**: `plugin/` is the shippable End-User AI Scaffold (distinct from this developer `.claude/` scaffold) — public-API-only skills for consumers of a deployed DataSpoke; repo root `.claude-plugin/marketplace.json` makes the repo a single-plugin marketplace. See `spec/AI_PLUGIN.md`. Claude-Code-specific packaging; not yet ported to other CLIs.
