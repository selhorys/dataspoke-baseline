# scaffold/ — agent-agnostic developer scaffold core

This directory is the single source of truth for DataSpoke's developer AI-scaffold content: what
each role (generator or evaluator) knows and does, independent of which coding-agent CLI drives
it. It exists so the plan → approve → generate → evaluate workflow (see root `AGENTS.md` and
`spec/AI_SCAFFOLD.md`) can run under more than one coding-agent CLI.

```
scaffold/
├── roles/    # canonical role definitions — one .md per generator/evaluator
├── memory/   # shared, version-controlled evaluator lessons
├── contracts/# structured evaluator output schemas
├── bin/      # explicit validation and binding-conformance utilities
└── README.md
```

## Two bindings read this core

- **Claude Code** (`.claude/agents/*.md`): generator bindings point to their live canonical roles.
  Read-only evaluator bindings require parent-supplied pre-generation authority and never reload
  live role or memory paths after generation.
- **Codex** (`.codex/agents/*.toml`): uses native project agents with explicit read-only evaluator
  and workspace-write generator sandboxes.

Both clients discover canonical skills under `.agents/skills/`; `.claude/skills` is a compatibility
link. Agent execution occurs only through native parent-coordinated agents.

## `roles/`

One file per role, extracted verbatim from the corresponding `.claude/agents/<name>.md` body:
before-you-start reading list, source layout, tech-stack conventions, invocation modes, and (for
evaluators) the scoring rubric + APPROVE/REVISE/ESCALATE verdict format. This is canonical —
`.claude/agents/*.md` no longer duplicates it.

## `memory/`

A flat-file `MEMORY.md` index plus note files for each evaluator role. This is the shared,
version-controlled, read-only SSOT for every client binding; `.claude/agent-memory` links here.

## Trusted review boundary

Before generation, the native parent captures evaluator bindings, canonical reviewer roles,
verdict contracts, and relevant evaluator memory from trusted repository state and loads evaluator
sessions from that snapshot. After generation and every fix pass, it supplies `Pinned evaluator
authority` and a separate `Untrusted per-pass evidence` payload containing complete status,
staged/unstaged diff, untracked-file, diff-check, and changed-path evidence. Evaluators never read
live authority paths. Actual paths determine security review.
Every verdict is schema- and semantics-validated; authority loss or invalid output escalates.

`bin/` contains explicit validation and binding-conformance utilities only. It does not execute
generators, reviewers, or workflows. Prauto retains its separate Claude workflow and security
model outside this interactive contract; see `spec/AI_PRAUTO.md`.

## Validation and permissions

The scaffold installs no automatic project lifecycle hooks. Its only automatic hooks are narrowly
scoped commit-approval guards. Claude Code's `PreToolUse` binding calls the shared
`hooks/protected-commit.py` classifier and requests its native approval UI when a recognized
`git commit` may target `dev` or `master`. Codex has no project hook: the Codex parent follows
`AGENTS.md` and invokes its native approval or user-question UI immediately before committing.
Matching Claude input, invocation, branch-resolution, and classifier faults block instead of
silently allowing a commit. Lint, typecheck, integration validation, and agent lifecycle gates
remain explicit commands rather than hooks. The statusline remains Claude-specific presentation.

The classifier is advisory and writable from the repository. It handles ordinary shell chaining,
directories, and wrappers conservatively, but it is heuristic rather than a full shell grammar. A
trusted checkout is required before enabling or accepting the hook. Native client policy remains
authoritative when shell syntax, aliases, functions, or another client falls outside the recognized
forms.
