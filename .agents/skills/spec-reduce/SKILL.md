---
name: spec-reduce
description: >-
  Audit and trim bloated specs, AI scaffold docs, and READMEs. Removes implementation
  details (code blocks, directory trees, method signatures), eliminates duplication
  across document tiers, and ensures each document stays at its appropriate abstraction
  level. Use when documents have grown fat from development iterations and need trimming
  back to architecture, decisions, and constraints.
disable-model-invocation: true
argument-hint: "[all | spec | scaffold | readme] [--dry-run]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

## Overview

Specs and harness documents accumulate implementation-level detail over time — verbatim code blocks, file-level directory trees, method signatures, and duplicate content across tiers. This skill audits target documents, identifies bloat, and trims them back to their intended abstraction level.

**When to use**: After a development phase has added significant implementation, or when document line counts have grown noticeably. This skill complements `spec-sync-with-impl` (which reconciles spec and code in either direction) and `spec-harmonize` (which propagates changes across specs). `spec-reduce` focuses on *removing excess detail* rather than adding missing content.

---

## Step 0 — Parse arguments

Parse `$ARGUMENTS` for scope and options.

| Argument | Meaning |
|----------|---------|
| `all` | Audit all tiers: specs, scaffold docs, and READMEs |
| `spec` | Only `spec/**/*.md` documents |
| `scaffold` | Only AI scaffold: `AGENTS.md`, `CLAUDE.md`, `spec/AI_SCAFFOLD.md`, `spec/AI_PRAUTO.md`, `scaffold/` (roles/memory/bin), and all `.claude/` components (agents, skills, settings) |
| `readme` | Only `README.md` files across the project |
| `--dry-run` | Report findings without making edits |

If no arguments provided, default to `all`.

---

## Step 1 — Collect inventory and measure

For the selected scope, collect all target documents with line counts:

```bash
wc -l <files> | sort -rn
```

### Target documents by tier

**Tier 1 — High-level specs** (target: architecture + decisions, minimal detail):
- `spec/ARCHITECTURE.md`
- `spec/MANIFESTO_en.md` / `spec/MANIFESTO_kr.md`
- `spec/API_DESIGN_PRINCIPLE_en.md` / `spec/API_DESIGN_PRINCIPLE_kr.md`
- `spec/DATAHUB_INTEGRATION.md`
- `spec/TESTING.md`
- `spec/USE_CASE_en.md` / `spec/USE_CASE_kr.md`

**Tier 2 — AI harness docs** (target: conventions + workflow, not code):
- `AGENTS.md`
- `CLAUDE.md`
- `spec/AI_SCAFFOLD.md`
- `spec/AI_PRAUTO.md`
- `scaffold/roles/*.md`, `scaffold/README.md`
- `.claude/agents/*.md`
- `.agents/skills/*/SKILL.md`
- `.claude/settings.json`, `.claude/settings.local.json`

**Tier 3 — Feature specs** (target: contracts + behavior, not implementation):
- `spec/feature/*.md`

**Tier 4 — READMEs** (target: quick-start + reference, pointers to specs):
- `README.md` (root)
- `helm-charts/README.md`
- `.prauto/README.md`
- `src/api/README.md`
- Any other `README.md` files found via glob

**Tier 5 — Evaluator memory** (target: dedup + trim padding, NOT architecture-doc brevity —
see [Memory-specific reduction principles](#memory-specific-reduction-principles), which
supersede G1-G4 for this tier):
- `scaffold/memory/{reviewer,security-reviewer,test-reviewer,spec-reviewer}/*.md` (every note,
  excluding the four `MEMORY.md` indexes themselves)

This tier was previously omitted from the inventory even though the `scaffold` scope argument
already named `scaffold/memory` — the gap let the corpus grow to 200+ notes / 8000+ lines with
no periodic size discipline. Always include it under `scaffold` and `all` scope.

---

## Step 2 — Audit each document

For each document, read and evaluate against the reduction principles below. Track findings as a list of `{file, issue_type, line_range, description, estimated_lines_saved}`.

### General reduction principles

These apply to ALL documents regardless of tier:

#### G1 — No verbatim code blocks that duplicate source files
Remove Python class definitions, method signatures, function bodies, and import examples that exist in the actual source code. Replace with a brief prose description or a "see `path/to/file`" pointer.

**Exception**: Short code snippets (< 5 lines) that illustrate a *convention or pattern decision* (not an implementation) may stay.

#### G2 — No file-level directory trees
Remove `tree`-style directory listings that enumerate every file with per-file comments. These become stale as soon as files are added or renamed — the filesystem itself is the source of truth.

**What to keep**: High-level module-purpose diagrams (3-5 entries showing relationships, not full file lists). Example: showing `src/api/ -> src/backend/ -> src/shared/` dependency flow is fine; listing every `.py` file is not.

#### G3 — No model/schema field definitions
Remove Pydantic model fields, SQLAlchemy column definitions, or dataclass fields that duplicate what's in code. Keep only fields that represent *design decisions* (e.g., a table explaining why certain fields exist, or a table mapping business concepts to storage choices).

#### G4 — No procedural narratives of code logic
Remove prose that narrates what code does step-by-step in a way that just restates the implementation. Keep *numbered pipeline steps* that describe the **what** and **why** at a workflow level, but not the **how** (function calls, variable names, control flow).

#### G5 — No duplicate command blocks
If a command appears in multiple documents, keep it in the most specific one and replace others with a pointer. Priority: README > spec > CLAUDE.md.

#### G6 — Tables over prose for structured data
If information is expressed as verbose paragraphs but could be a table, it should be a table. Tables are more scannable and take fewer lines.

### Deduplication principles — what belongs where

These define which tier owns which content. When the same information appears in multiple tiers, keep it in the **owning tier** and replace others with a one-line pointer.

#### D1 — Specs own architecture, decisions, and constraints
- Spec documents are the authority for *why* things are designed a certain way
- They should describe contracts (input/output shapes, error codes, flow steps) but not implementations
- Feature specs own the detailed behavior; high-level specs own the system-wide patterns

#### D2 — Code owns implementation details
- Method signatures, class definitions, directory structures, and configuration defaults belong in source code
- Specs should reference code paths, not reproduce them

#### D3 — READMEs own quick-start and operational how-to
- READMEs provide the shortest path to "get it running"
- They should contain install/run/verify commands and credential tables
- For deeper explanation, they should point to the relevant spec
- READMEs should NOT duplicate content from other READMEs — each README serves its own directory scope

#### D4 — CLAUDE.md owns agent behavioral rules
- Rules that affect how Claude Code behaves in this repo (commit conventions, test execution groups, pre-flight checks)
- Should be concise — detailed testing procedures belong in TESTING.md, not CLAUDE.md
- CLAUDE.md may summarize and point to specs, but should not duplicate their content

#### D5 — Agent files own role-specific instructions
- `.claude/agents/*.md` should contain only what the agent needs beyond what CLAUDE.md and specs provide
- Directory trees and code examples in agent files should be minimal — the agent can read the code itself

#### D6 — Cross-document duplication resolution

When the same content appears in multiple places, use this priority to decide where it stays:

| Content type | Primary owner | Others get |
|---|---|---|
| Architecture diagrams | `spec/ARCHITECTURE.md` | One-line pointer |
| API route catalogue | `spec/API.md` | One-line pointer |
| Service responsibilities | `spec/feature/BACKEND.md` | Brief summary (1-2 lines) |
| Directory trees | Nowhere (read the filesystem) | Remove entirely |
| Test execution groups | `spec/TESTING.md` + `CLAUDE.md` summary | Pointer to TESTING.md |
| Ingress endpoints table | `helm-charts/README.md` | Brief list in root README |
| Lock API reference | `helm-charts/README.md` | Pointer |
| Dummy data details | `spec/TESTING.md` §Test Data | Pointer |
| Prauto directory structure | Nowhere (read `.prauto/`) | Brief prose description |
| Helm chart config | `spec/feature/HELM_CHART.md` | Pointer |

---

### Memory-specific reduction principles

`scaffold/memory/**/*.md` notes are an evaluator's evidence trail (exact measured numbers, exact
repro commands, exact `file:line` citations), not an architecture doc for readers. **G1-G4 do NOT
apply to this tier** — a measured command, a specific vector, or a precise number is the finding,
not implementation detail to strip. Applying G1-G4 here would gut the thing that makes a memory
note worth more than a spec sentence. Use these principles instead:

#### M1 — Trim restated/padding prose, keep evidence
Remove throat-clearing, scene-setting, and sentences that just restate the finding in different
words. Keep: exact measured numbers, exact repro commands/vectors, exact `file:line` citations,
the causal mechanism ("why"), and the "how to apply" trigger condition.

#### M2 — Dedup against canonical docs
If the fact (or an equivalent) now lives in `spec/*.md` or `scaffold/roles/*.md`, trim the note to
a short pointer plus only the residual evidence/incident detail the canonical doc doesn't carry —
never restate what the canonical doc already says in full.

#### M3 — Compress narrative to the causal chain
A paragraph narrating "I first tried X, then realized Y, then discovered Z" compresses to the
causal chain that matters for re-verification — not a travelogue of the investigation.

#### M4 — Merge overlapping Why/How-to-apply boilerplate
When a note's "Why" and "How to apply" sections restate each other, collapse to the shorter form
without losing the trigger condition (when does this apply) or the fix.

#### M5 — Never delete a note that has an inbound `[[wiki-link]]`
`scaffold/bin/check-bindings.sh` asserts every `[[wiki-link]]` resolves to some note's frontmatter
`name:`. Before removing a file outright, grep the whole corpus for `[[<its-name>]]` — if any
other note references it, keep it as a stub (frontmatter + 2-4 lines) rather than deleting it.
A note with zero inbound references may be deleted outright if its content is fully promoted or
genuinely superseded.

#### M6 — Preserve verbatim
Exact numbers/measurements, `file:line` citations, command/vector reproductions, and every
`[[wiki-link]]` cross-reference and frontmatter `name:`/`description:` pair (update `description:`
only if the trimmed body changes what the note is actually about).

---

## Step 3 — Report findings

Present findings to the user as a summary table:

```
## Spec Reduction Audit

| Document | Lines | Issues | Est. reduction |
|----------|-------|--------|----------------|
| spec/feature/BACKEND.md | 1394 | G1(5), G2(1), G3(2) | ~800 lines |
| spec/TESTING.md | 640 | G4(2), G5(3), D6(1) | ~250 lines |
| ... | ... | ... | ... |

### Top issues

1. **spec/feature/BACKEND.md** — 5 verbatim Python class definitions (G1), ...
2. ...
```

If `--dry-run` was specified, stop here and let the user decide which to proceed with.

---

## Step 4 — Apply reductions

For each document with findings, apply reductions in order of impact (largest savings first). For each change:

1. **Remove** the identified bloat (code block, directory tree, duplicate section)
2. **Replace** with appropriate content:
   - For code blocks: brief prose description or "see `path/to/file`" pointer
   - For directory trees: one-sentence description of the module's purpose
   - For duplicate content: "See [Document §Section](path)" pointer
   - For verbose prose: condensed table or bullet list
3. **Preserve** all architecture decisions, design rationale, constraint descriptions, and reference tables

### Quality checks during reduction

- Never remove a table that captures a *design decision* (error code mappings, quality score weights, concurrency guard rules, etc.)
- Never remove numbered pipeline steps that describe workflow sequencing
- Never remove sections marked TBD/TODO — these represent planned work
- Verify that all internal markdown links (`[text](path)` and `[text](path#anchor)`) still resolve after edits
- Keep the Table of Contents in sync with remaining sections
- For Tier 5 (`scaffold/memory/`): never remove a note's exact measurement, command, or
  `file:line` citation (M6); never delete a note with an inbound `[[wiki-link]]` (M5) — trim to a
  stub instead; after all edits, run `bash scaffold/bin/check-bindings.sh` and confirm it still
  passes (it asserts no dangling `[[wiki-link]]` and no unsuperseded reference to a deleted
  mechanism)

---

## Step 5 — Verify

After all edits:

1. **Line count comparison**: `wc -l` before vs after for each modified file
2. **Cross-reference check**: Verify no broken markdown links across modified files
3. **Git diff stat**: Show the overall reduction

Present the final summary to the user:

```
## Reduction complete

| Document | Before | After | Reduction |
|----------|--------|-------|-----------|
| ... | ... | ... | ...% |

Total: X files, -Y lines (Z% reduction)
No broken cross-references found.
```
