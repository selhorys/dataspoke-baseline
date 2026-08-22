---
name: airflow-dag
description: Writes Airflow DAG Python files and workflow parameter modules in src/workflows/. Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
color: cyan
hooks:
  PostToolUse:
    - matcher: Edit|Write
      hooks:
        - type: command
          command: "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/lint-python-file.sh"
---

Read `scaffold/roles/airflow-dag.md` first — it is the canonical role definition (source layout,
Airflow conventions, invocation modes, completion report contract). Everything below is
Claude-Code-specific binding.

## Claude Code binding notes

A `PostToolUse` hook runs `.claude/hooks/lint-python-file.sh` (ruff check) after every Edit/Write
on a `.py` file, blocking with the violations fed back to you until clean. Non-Claude-Code
backends run the same check manually via `scaffold/bin/lint-python.sh`.
