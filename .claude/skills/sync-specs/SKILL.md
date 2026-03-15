---
name: sync-specs
description: >-
  Propagate spec changes to sibling/parent specs and harness docs.
  Use when a spec is created, modified, or deleted and dependent documents need updating —
  including updating references in ARCHITECTURE.md, README.md, CLAUDE.md, or USE_CASE;
  adding new specs to feature mapping tables; fixing cross-references between related specs;
  or removing dead links. Invoke this skill whenever you need to "update parent/sibling spec references",
  "keep specs consistent", or "propagate changes up/down the spec hierarchy",
  even without an explicit user request.
disable-model-invocation: true
user-invocable: true
argument-hint: <spec-file-path> [new|modified|deleted]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

## Overview

Spec change propagation: when one spec changes, update all documents that reference or list it. `sync-specs` syncs **within the spec layer**.

---

## Step 0 — Parse arguments and detect change

Parse `$ARGUMENTS` for the changed file path and optional change type.

### If arguments are provided

Extract:
- **File path** — the spec that changed (e.g., `spec/feature/SEARCH.md`)
- **Change type** — `new`, `modified`, or `deleted` (optional)

### If no arguments are provided

Ask the user:

```
Which spec file changed, and how?

  Usage: /sync-specs <spec-file-path> [new|modified|deleted]

  Examples:
    /sync-specs spec/feature/SEARCH.md new
    /sync-specs spec/ARCHITECTURE.md modified
    /sync-specs spec/feature/spoke/DE_VALIDATOR.md deleted
```

Wait for the user's reply before proceeding.

### Auto-detect change type

If the change type is not specified, detect it from git:

```bash
git status --porcelain -- <file-path>
```

- `??` or `A` → `new`
- `M` → `modified`
- `D` → `deleted`
- Clean (no output) → assume `modified` (already committed)

Read the changed spec (unless deleted) to understand what it adds, modifies, or removes.

---

## Step 1 — Determine spec priority level

Classify the changed file using the spec hierarchy from `CLAUDE.md`:

| Priority | Pattern | Documents |
|----------|---------|-----------|
| 1 | `spec/MANIFESTO_*.md` | Product identity |
| 2 | `spec/API_DESIGN_PRINCIPLE_*.md`, `spec/DATAHUB_INTEGRATION.md` | Binding conventions |
| 3 | `spec/ARCHITECTURE.md`, `spec/TESTING.md`, `spec/USE_CASE_*.md` | System architecture |
| 4 | `spec/AI_SCAFFOLD.md`, `spec/AI_PRAUTO.md` | Scaffold conventions |
| 5 | `spec/feature/*.md` | Common feature specs |
| 6 | `spec/feature/spoke/*.md` | User-group-specific specs |
| — | `CLAUDE.md`, `README.md`, `.claude/**` | Harness documents |

The priority level determines which other documents may need updating and which require user confirmation.

---

## Step 2 — Identify target documents

Based on the changed spec and its priority level, identify all documents that should reference or list it.

### Propagation matrix

| Target Document | Updated when... |
|----------------|-----------------|
| `spec/ARCHITECTURE.md` | Feature-to-architecture mapping table, repository structure, component lists change |
| `CLAUDE.md` | New priority-level doc added, spec hierarchy table needs update, key reference pointers change |
| `README.md` (root) | Documentation table, features list, or repository structure change |
| `spec/AI_SCAFFOLD.md` | `.claude/` structure changes, skill/subagent tables need update |
| `spec/USE_CASE_en.md` | Cross-references to feature specs need adding/removing |
| Other `spec/feature/*.md` | Cross-references between related features |
| Component `README.md` files | Links to relevant specs |

### Rules for which targets to check

- **New spec (priority 5–6)**: Check ARCHITECTURE.md (feature mapping), README.md (documentation table), USE_CASE_en.md (cross-refs), and sibling feature specs (cross-refs).
- **New spec (priority 1–4)**: Check CLAUDE.md (spec hierarchy), README.md (documentation table), AI_SCAFFOLD.md (if scaffold-related).
- **Modified spec**: Check all targets that currently reference the changed file for stale content.
- **Deleted spec**: Check all targets for dead references that need removal.

Read each target document to scan for existing references to the changed spec.

---

## Step 3 — Build propagation report

Compare the changed spec content against each target document. Identify:

| Category | Description |
|----------|-------------|
| **Missing reference** | Target should list/link the changed spec but doesn't |
| **Stale reference** | Target references old name, path, or description |
| **Dead reference** | Target references a deleted spec |
| **Outdated table entry** | Table row has wrong description, path, or classification |
| **Missing cross-reference** | Related feature specs don't reference each other |

Present findings:

```
## Sync-Specs Report — <changed-file>

### Change Summary
- File: <path>
- Type: <new|modified|deleted>
- Priority level: <N>

### Propagation Targets

| # | Target File | Issue | Proposed Action |
|---|-------------|-------|-----------------|
| 1 | README.md | Missing from documentation table | Add row to Documentation table |
| 2 | spec/ARCHITECTURE.md | Not in feature mapping | Add to feature-architecture mapping |
| ... | ... | ... | ... |

### No Changes Needed
- <list of checked documents that are already up to date>
```

---

## Step 4 — Apply changes with confirmation

### Confirmation rules

Follow the spec hierarchy for confirmation requirements:

| Target priority | Confirmation |
|----------------|-------------|
| Priority 1 (MANIFESTO) | **Never modify.** Only flag contradictions for user review. |
| Priority 2–3 | **Always ask** before applying. Show diff preview first. |
| Priority 4 | **Ask** before applying. Show proposed changes. |
| Priority 5–6, README, CLAUDE.md | **Ask** before applying. May batch multiple changes in one confirmation. |

### Apply process

1. Show the propagation report from Step 3.
2. Ask the user to confirm each group of changes (batch by target file).
3. Apply edits using the `Edit` tool (prefer surgical edits over full rewrites).
4. After applying, re-read each modified file to verify the edit landed correctly.

---

## Step 5 — Verify consistency

After all edits are applied:

1. **Check for circular references** — ensure no document references itself incorrectly.
2. **Check cross-document consistency** — verify that names, paths, and descriptions match across all updated documents.
3. **Report completion** — summarize what was changed and what was left unchanged.

```
## Sync Complete

### Files Modified
- README.md: Added SEARCH.md to documentation table
- spec/ARCHITECTURE.md: Added search feature to feature mapping

### Files Unchanged (already consistent)
- CLAUDE.md
- spec/USE_CASE_en.md

### Manual Review Suggested
- <any items flagged for user attention>
```
