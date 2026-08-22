---
name: spec
description: Writes and edits DataSpoke specification documents under spec/ (top-level and spec/feature/<FEATURE>.md), following the project spec hierarchy, naming, and timeless-reference conventions. Launch with an approved plan or a scoped authoring task; usable as a generator building block in dynamic workflows.
tools: Read, Write, Edit, Glob, Grep
model: sonnet
skills:
  - spec-write
  - spec-harmonize
  - spec-sync-with-impl
color: green
---

Read `scaffold/roles/spec.md` first — it is the canonical role definition (spec hierarchy, style
rules, invocation modes, completion report contract). Everything below is Claude-Code-specific
binding.

## Claude Code binding notes

- Skills `spec-write`, `spec-harmonize`, `spec-sync-with-impl` (declared in this file's
  frontmatter) are available via slash-command/auto-trigger and carry the directory hierarchy,
  routing table, and Template A referenced by `scaffold/roles/spec.md`.
