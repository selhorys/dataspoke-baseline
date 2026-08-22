---
name: test
description: Writes and runs tests for DataSpoke across all layers (unit, spot integration, api-wired integration, E2E). Launch only with an approved implementation plan, or for a test-reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
color: purple
hooks:
  PostToolUse:
    - matcher: Edit|Write
      hooks:
        - type: command
          command: "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/lint-python-file.sh"
---

Read `scaffold/roles/test.md` first — it is the canonical role definition (test directory layout,
testing rules, E2E conventions, test-quality checklist, completion report contract). Everything
below is Claude-Code-specific binding.

## Claude Code binding notes

A `PostToolUse` hook runs `.claude/hooks/lint-python-file.sh` (ruff check) after every Edit/Write
on a `.py` file, blocking with the violations fed back to you until clean. Non-Claude-Code
backends run the same check manually via `scaffold/bin/lint-python.sh`. Also, before running
integration tests, `.claude/hooks/preflight-integration-tests.sh` runs
`helm-charts/bin/health-check.sh` and blocks on an unhealthy cluster — the same gate holds at the
pytest layer for any caller via `tests/integration/conftest.py`'s `require_server` fixture.
