---
name: backend
description: Writes FastAPI/Python backend code for DataSpoke across src/api/, src/backend/, and src/shared/. Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
skills:
  - datahub-api
color: blue
hooks:
  PostToolUse:
    - matcher: Edit|Write
      hooks:
        - type: command
          command: "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/lint-python-file.sh"
---

Read `scaffold/roles/backend.md` first — it is the canonical role definition (source layout, tech
stack rules, invocation modes, completion report contract). Everything below is Claude-Code-specific
binding.

## Claude Code binding notes

- Skill `datahub-api` (declared in this file's frontmatter) is available via slash-command/auto-trigger.
- A `PostToolUse` hook runs `.claude/hooks/lint-python-file.sh` (ruff check) after every Edit/Write
  on a `.py` file, blocking with the violations fed back to you until clean. Non-Claude-Code
  backends run the same check manually via `scaffold/bin/lint-python.sh`.
