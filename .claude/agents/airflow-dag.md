---
name: airflow-dag
description: Writes Airflow DAG Python files in src/workflows/dags/ and maintains their support modules (src/workflows/airflow/, _common.py, registry.py). Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
effort: high
color: cyan
---

Read `scaffold/roles/airflow-dag.md` first — it is the canonical role definition (source layout,
Airflow conventions, invocation modes, completion report contract). Everything below is
Claude-Code-specific binding.

## Claude Code binding notes

Run Python lint explicitly with `scaffold/bin/lint-python.sh` during verification.
