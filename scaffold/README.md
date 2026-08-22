# scaffold/ — agent-agnostic developer scaffold core

This directory is the single source of truth for DataSpoke's developer AI-scaffold content: what
each role (generator or evaluator) knows and does, independent of which coding-agent CLI drives
it. It exists so the plan → approve → generate → evaluate workflow (see root `AGENTS.md` and
`spec/AI_SCAFFOLD.md`) can run under more than one coding-agent CLI.

```
scaffold/
├── roles/    # canonical role definitions — one .md per generator/evaluator
├── memory/   # persistent cross-session lessons for evaluator roles (non-Claude-Code backends)
├── bin/      # scripts that drive a role or a full stage loop via a chosen backend CLI
└── README.md
```

## Two bindings read this core

- **Claude Code** (`.claude/agents/*.md`): each subagent file keeps its Claude-Code-specific
  frontmatter (`tools:`, `model:`, `hooks:`, `memory:`, `skills:`) and points to the matching
  `scaffold/roles/<name>.md` as the canonical role definition. Claude Code's native `Agent` tool
  and `.claude/workflows/wf-minimal.js` drive delegation and the review loop directly — nothing
  in `scaffold/bin/` is needed for a Claude Code session.
- **Codex** (or any other CLI without a built-in subagent/hook/workflow primitive): reads root
  `AGENTS.md` automatically, and drives one role at a time via `scaffold/bin/run-stage.sh`, or a
  full stage loop via `scaffold/bin/run-workflow.sh` — a bash port of `wf-minimal.js`'s
  generate → evaluate state machine, since Codex has no native equivalent to Claude Code's
  `Agent`/`Workflow` tools.

## `roles/`

One file per role, extracted verbatim from the corresponding `.claude/agents/<name>.md` body:
before-you-start reading list, source layout, tech-stack conventions, invocation modes, and (for
evaluators) the scoring rubric + APPROVE/REVISE/ESCALATE verdict format. This is canonical —
`.claude/agents/*.md` no longer duplicates it.

## `memory/`

A flat-file `MEMORY.md` index + note-file convention (same shape as this project's own
Claude Code session memory) for each evaluator role, used only by non-Claude-Code backends.
Claude Code's own `memory: project` frontmatter field auto-loads `.claude/agent-memory/<name>/`
instead — that store is untouched and unrelated to this one. A Codex-driven evaluator session
follows the explicit read-before/append-after instruction in its `scaffold/roles/<name>.md` file
to build up its own, separate memory here over time.

## `bin/`

- `run-stage.sh <role> <plan-file> --agent {claude|codex} [--input FILE] [--model NAME]` —
  invoke one role once, non-interactively, via the chosen backend CLI.
- `run-workflow.sh <plan-file> --agent {claude|codex} [--security s1,s2] <stage> [<stage> ...]` —
  drive a full generate → evaluate stage loop (one fix pass on REVISE, escalate on persistent
  REVISE or any ESCALATE) across an ordered list of stages.
- `lint-python.sh <file.py>` — the `ruff check` logic behind `.claude/hooks/lint-python-file.sh`,
  as a plain script any role's self-verification step can call directly.

The Codex adapter in `run-stage.sh` has its exact CLI flags marked TBD — this repo did not have
`codex` installed when it was written. Run `codex exec --help` against an installed Codex CLI and
adjust `invoke_codex()` before relying on the `--agent codex` path.

## What deliberately isn't here

Three Claude Code conveniences have no portable equivalent and aren't ported — see
`spec/AI_SCAFFOLD.md §Codex Binding` for the rationale: the statusline (pure CLI chrome), the
commit-message-format check in `confirm-commit.sh` (documented as a convention in `AGENTS.md`
instead), and `permission-hygiene-check.sh` (specific to `.claude/settings.local.json`).
`.claude/agent-memory/` (Claude Code's own store) is left as-is, not migrated here.
