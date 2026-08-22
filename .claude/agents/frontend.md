---
name: frontend
description: Writes Next.js + TypeScript frontend code for DataSpoke in src/frontend/. Launch only with an approved implementation plan, or for a reviewer-directed fix pass.
tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
color: blue
---

Read `scaffold/roles/frontend.md` first — it is the canonical role definition (source layout, tech
stack rules, file naming, invocation modes, completion report contract). Everything below is
Claude-Code-specific binding.

## Claude Code binding notes

Run `pnpm -C src/frontend typecheck` explicitly before completing. The package's `pretest` script
also chains typecheck ahead of tests.
