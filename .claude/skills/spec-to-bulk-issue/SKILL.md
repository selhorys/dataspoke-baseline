---
name: spec-to-bulk-issue
description: >-
  Analyze spec documents to find unimplemented components, write ordered issue
  tickets as markdown files in issues/, revise existing issue files against
  current specs and implementation, and optionally register them to GitHub
  with the prauto:ready label. Use when the user wants to bulk-create
  implementation issues from specs, or revise existing issues.
argument-hint: "write [<spec-path> ...] | revise [<glob>] | register [<glob>]"
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(gh *), Bash(mkdir *), Bash(bash *register-issues*)
---

## Overview

This skill has three modes invoked by the user:

| Mode | What it does |
|------|-------------|
| `write` | Read specs, diff against implementation, produce `issues/` markdown files |
| `revise` | Re-read specs + implementation, update existing `issues/` files to reflect current state |
| `register` | Push existing `issues/*.md` files to GitHub as issues with `prauto:ready` |

If `$ARGUMENTS` is empty or ambiguous, ask the user which mode to run.

---

## Mode: write

### Step 1 — Determine target specs

Parse `$ARGUMENTS` for spec file paths (e.g. `spec/feature/BACKEND.md spec/feature/BACKEND_SCHEMA.md`).
If none provided, ask the user which specs to analyze.

Also read related specs for cross-reference context:
- `spec/ARCHITECTURE.md` — system components, feature-to-architecture mapping
- `spec/feature/API.md` — route catalogue (to know which API routes exist in spec)
- Any other specs explicitly referenced by the target specs

### Step 2 — Inventory what exists

Glob for existing implementation files:

```
src/**/*.py
src/**/*.ts
tests/**/*.py
migrations/**/*
api/**/*.yaml
```

Read the key implementation files (especially `src/api/main.py`, router files, service files) to understand what is already implemented vs. what returns stubs/501s.

### Step 3 — Gap analysis

For each component described in the target specs, classify as:
- **Implemented** — real logic exists in code
- **Stub** — file/route exists but returns 501, raises NotImplementedError, or has TODO markers
- **Missing** — no corresponding file or code exists at all

Only Stub and Missing components become issue candidates.

### Step 4 — Group into issues

Group related components into cohesive issues (5–20 issues total). Grouping principles:
- One issue per service module (e.g. `src/backend/validation/` = one issue)
- Shared infrastructure can be grouped (e.g. all 4 infra clients in one issue)
- Split large concerns (e.g. validation scoring vs. anomaly detection)
- Each issue should be independently implementable given its dependencies

### Step 5 — Dependency ordering

Build a dependency graph between issues and assign order numbers:
- Foundation modules (no internal deps) get lowest numbers
- Modules that depend on others get higher numbers
- Parallelizable issues at the same layer share the same priority tier but get sequential numbers

### Step 6 — Write issue files

Create `issues/` directory if it doesn't exist:
```bash
mkdir -p issues
```

Write one markdown file per issue: `issues/{NN}_{slug}.md`

**File format** — must match the rendered structure of `.github/ISSUE_TEMPLATE/prauto-task.yml`:

```markdown
# Title

<type>: <scope> - <short description>

# Body

### Spec Scope

- [ ] Architecture or high-level spec (spec/)
- [ ] Cross-role feature spec (spec/feature/)
- [ ] Role-specific feature spec (spec/feature/spoke/)
- [ ] General documentation or settings (README.md, CLAUDE.md, .claude/, etc)
- [ ] Tooling or CI/CD (PRAUTO, GitHub Actions, hooks, etc.)
- [ ] No spec change (implementation only)
- [ ] Other (documented in description)

### Implementation Scope

- [ ] OpenAPI spec (api/)
- [ ] Backend - API routes and services
- [ ] Backend - Temporal workflows
- [ ] Backend - DataHub integration, PostgreSQL models, Qdrant vector search
- [ ] Frontend UI
- [ ] Helm charts / Kubernetes manifests
- [ ] Dev environment
- [ ] Tooling or CI/CD (PRAUTO, GitHub Actions, hooks, etc.)
- [ ] No impl change (spec only)
- [ ] Other (documented in description)

### Change Size

<Minor | Medium | Major> (<parenthetical>)

### Description

#### Background

<1-3 sentences: why this is needed, dependency references>

#### Spec

<Bullet list: key components, interfaces, data models from spec>

#### TODO

- [ ] implement `<path>`
- [ ] unit test: <what>
- [ ] integration test: <what> (if needed)

### Spec References

- <spec-file> (section: <section name>)
```

**Constraints:**
- Each file should be under 30 lines in the Body section (excluding Title). Keep descriptions concise — logic details are in the spec documents, so issue tickets only need to specify *which* spec components are implemented and *how* they are tested.
- Mark the correct checkboxes for Spec Scope and Implementation Scope.
- Title follows conventional commits: `feat: <scope> - <description>`
- Check the appropriate boxes (use `[x]`) — don't leave all unchecked.

### Step 7 — Summary

Print a summary table showing:
- Issue number, filename, title
- Dependencies (which issues it depends on)
- Change size

---

## Mode: revise

### Step 1 — Discover existing issues

Glob for `issues/*.md` files. If `$ARGUMENTS` contains a specific glob or file list, use that subset instead.

Read each issue file. Extract from each:
- Title and slug
- Spec References section → list of spec files and sections
- TODO items → list of implementation paths
- Dependencies (from Background text, e.g. "Depends on #03")

### Step 2 — Re-read specs

Collect the union of all spec files referenced across the issue set. Read each spec file.

Also read cross-reference context:
- `spec/ARCHITECTURE.md`
- `spec/feature/API.md`
- Any other specs explicitly referenced by the target specs

### Step 3 — Re-inventory implementation

Same as write mode Step 2: glob and read implementation files to determine current state.

### Step 4 — Per-issue diff

For each existing issue, compare its TODO items and Spec bullet points against current spec and implementation:

- **Completed TODOs**: implementation now exists with real logic → check off or remove the TODO item
- **Stale spec points**: spec section was rewritten, renamed, or removed → update Spec bullets and Spec References
- **New spec points**: spec added components not covered by any existing issue → note for potential new issue
- **Scope drift**: issue description no longer matches spec (renamed paths, changed interfaces) → update Description
- **Dependency changes**: a depended-on issue was split, merged, or renumbered → update Background references

Classify each issue as:
- **No change** — still accurate
- **Update** — needs content edits
- **Drop** — fully implemented or spec removed (flag for user confirmation)
- **Split/merge** — scope changed significantly (flag for user confirmation)

### Step 5 — Apply edits

For issues classified as **Update**: edit the issue file in-place, preserving the `# Title` / `# Body` structure and template format. Keep changes minimal — only update what actually changed.

For issues classified as **Drop** or **Split/merge**: print a recommendation and ask the user before deleting or restructuring files.

If new spec components are found that aren't covered by any existing issue, write new issue files following the same format and numbering convention (use the next available number prefix).

### Step 6 — Summary

Print a summary table:
- Issue filename, title
- Status: `unchanged | updated | dropped | new`
- What changed (one-line description per updated issue)

---

## Mode: register

### Step 1 — Discover issue files

Glob for `issues/*.md` files. If `$ARGUMENTS` contains a specific glob or file list, use that instead.

Sort files by their numeric prefix (ascending) so earlier-order issues are registered first.

### Step 2 — Run the helper script

Use the helper script at `.claude/skills/spec-to-bulk-issue/register-issues.sh` which handles auth verification, title/body parsing, and sequential `gh issue create` calls:

```bash
bash .claude/skills/spec-to-bulk-issue/register-issues.sh <file1> <file2> ...
```

The script:
1. Verifies `gh auth status`
2. Resolves the repo name via `gh repo view`
3. For each file (in argument order): extracts title (line 3), body (everything after `# Body`), calls `gh issue create --label "prauto:ready"`
4. Prints a summary table: filename, issue number, URL

### Step 3 — Summary

Relay the script's summary table to the user. If any files were skipped, note why.

