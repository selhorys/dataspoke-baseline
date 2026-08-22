---
name: frontend
description: Writes Next.js + TypeScript frontend code for DataSpoke in src/frontend/. Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
color: blue
hooks:
  Stop:
    - hooks:
        - type: command
          command: "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/typecheck-frontend.sh"
---

Read `scaffold/roles/frontend.md` first — it is the canonical role definition (source layout, tech
stack rules, file naming, invocation modes, completion report contract). Everything below is
Claude-Code-specific binding.

## Claude Code binding notes

A `Stop` hook runs `.claude/hooks/typecheck-frontend.sh` (`pnpm -C src/frontend typecheck`),
blocking completion until it passes. `src/frontend/package.json`'s `pretest` script also chains
typecheck ahead of `pnpm -C src/frontend test`, so the same guarantee holds for any caller
(human, Codex, CI) — the hook here is only an earlier, Claude-Code-specific fail-fast.
