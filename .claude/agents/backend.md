---
name: backend
description: Writes FastAPI/Python backend code for DataSpoke across src/api/, src/backend/, and src/shared/. Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
skills:
  - datahub-api
color: blue
---

Read `scaffold/roles/backend.md` first — it is the canonical role definition (source layout, tech
stack rules, invocation modes, completion report contract). Everything below is Claude-Code-specific
binding.

## Claude Code binding notes

- Skill `datahub-api` (declared in this file's frontmatter) is available via slash-command/auto-trigger.
- Run Python lint explicitly with `scaffold/bin/lint-python.sh` during verification.
