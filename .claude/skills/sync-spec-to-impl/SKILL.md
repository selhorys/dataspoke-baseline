---
name: sync-spec-to-impl
description: Synchronize DataSpoke specification documents with current implementation state. Use when specs and implementations have drifted and need reconciliation.
argument-hint: [prauto|ai-scaffold|dev-env|helm-charts|api|ref|backend|frontend|all]
allowed-tools: Read, Write, Edit, Glob, Grep
---

## Definitions

### What counts as Specification

Specification is any document that describes *what should exist and how it should behave*:

- Docs under `spec/` and `spec/feature/` directories
- `README.md` files at each component directory and at the repository root
- `CLAUDE.md` at the repository root
- **Declarations** in SKILL.md files (YAML frontmatter + description/routing sections at the top — everything above the detailed workflow instructions)

### What counts as Implementation

Implementation is the actual code, scripts, and configs that *do things*:

- `.prauto/` scripts, libraries, prompts, and config files
- `dev_env/` scripts, manifests, and helpers
- `helm-charts/` chart definitions, values, and templates
- `api/` OpenAPI spec (`openapi.yaml`)
- `src/` application code (api, backend, frontend) and `tests/` directories
- **Detailed logic** in SKILL.md files (the workflow instruction body — everything below the declarations)
- `ref/` system (reference materials and setup scripts)

### The boundary inside SKILL.md

A single SKILL.md file straddles both sides:

| Part | Counts as | What it contains |
|------|-----------|------------------|
| YAML frontmatter (`name`, `description`, `argument-hint`, `allowed-tools`, flags) | **Spec** | What the skill claims to do |
| Routing table / first overview section | **Spec** | How the skill is invoked |
| Detailed step-by-step workflow instructions | **Impl** | How the skill actually works |

---

## Routing & Scope Definitions

Parse `$ARGUMENTS` to determine the change scope. If no arguments are given or the scope is unclear, output the following scope menu and **wait for the user to reply** with their selection:

```
Select scopes to sync (comma-separated numbers or keywords):

  1. all          — All scopes that have both spec and impl files
  2. prauto       — .prauto scripts, prauto-related specs and skills
  3. ai-scaffold  — CLAUDE.md, .claude/ settings, hooks, agents, all skills
  4. dev-env      — dev_env/ scripts, DEV_ENV spec
  5. helm-charts  — Helm chart definitions and specs
  6. api          — API specs, OpenAPI, src/api/ code
  7. ref          — ref/ setup scripts and reference materials
  8. backend      — Backend services, Temporal workflows (TBD)
  9. frontend     — Next.js frontend code (TBD)

Example: "1" or "prauto, api" or "2,3,6"
```

Parse the user's reply into individual scope keywords. Accept numbers, keywords, or a mix.

The directories below are **starting points** — always glob the actual tree to discover what exists. If files have moved or been renamed, follow the real structure rather than these hints.

| Scope keyword | Spec side | Impl side |
|---------------|-----------|-----------|
| `prauto` | `spec/AI_PRAUTO.md`, prauto-related skill declarations, `.prauto/README.md` | `.prauto/` scripts and libs, prauto-related skill logic bodies |
| `ai-scaffold` | `spec/AI_SCAFFOLD.md`, `CLAUDE.md`, all skill declarations across `.claude/skills/` | `.claude/` settings/hooks/agents, all skill logic bodies |
| `dev-env` | `spec/feature/DEV_ENV.md`, dev-env skill declaration, `dev_env/README.md` | `dev_env/` scripts and helpers, dev-env skill logic body |
| `helm-charts` | `spec/feature/HELM_CHART.md` | `helm-charts/` charts, values, and templates |
| `api` | `spec/feature/API.md`, `spec/API_DESIGN_PRINCIPLE_en.md`, `src/api/README.md` | `api/` OpenAPI spec, `src/api/` routers/schemas/auth/middleware |
| `ref` | `spec/AI_SCAFFOLD.md` (ref section), ref-setup skill declaration, `ref/README.md` | `ref/` setup scripts and reference materials, ref-setup skill logic body |
| `backend` | TBD | TBD |
| `frontend` | TBD | TBD |
| `all` | All of the above | All of the above |

- If the user selects `all`, expand to every scope that has both spec and impl files present.
- For scopes marked **TBD**: inform the user that the mapping is not yet defined and skip with a note.

---

## Step 1 — Collect inventory

For each selected scope:

1. **Glob** for all spec-side and impl-side files using the areas in §Routing & Scope Definitions.
2. Build a file inventory: `{scope, side, path, exists}`.
3. If a scope has no spec files or no impl files, report it and skip with a warning.

---

## Step 2 — Read and compare

For each scope, read files on both sides and identify discrepancies.

### What to look for

| Category | Example discrepancies |
|----------|-----------------------|
| **Structural drift** | Spec lists components/scripts that don't exist in impl, or impl has components not mentioned in spec |
| **Naming mismatch** | Spec uses one name for a concept; impl uses a different name |
| **Behavioral drift** | Spec describes a workflow or protocol; impl does it differently |
| **Missing documentation** | Impl has features/options not documented in spec or README |
| **Stale references** | Spec references files, paths, or versions that have changed |
| **Skill declaration drift** | SKILL.md declaration (frontmatter/routing) doesn't match the skill's logic body |

### Reading strategy

- **Spec side**: Read fully — these define the intended behavior.
- **Impl side**: Focus on structure, exported functions/endpoints, CLI options, and high-level flow. Don't read every line of business logic unless the spec describes that level of detail.
- **SKILL.md**: Read the declaration (frontmatter + routing) as spec; skim the logic body for workflow steps and behavioral contracts; compare the two sides against each other.

---

## Step 3 — Classify and prioritize changes

### High-level spec rules (spec hierarchy awareness)

Follow the spec hierarchy from `CLAUDE.md`. Higher-priority documents constrain lower ones.

| Priority | Documents | Sync rule |
|----------|-----------|-----------|
| 1 | `MANIFESTO_en/kr.md` | **Never modify.** Only flag contradictions for user review. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Fix factual inaccuracies only. Keep conventions abstract — no impl details. |
| 3 | `ARCHITECTURE.md`, `TESTING.md`, `USE_CASE_en/kr.md` | Update component lists, tech stack, data flows if they've changed. Keep architectural level — no code-level detail. |
| 4 | `AI_SCAFFOLD.md`, `AI_PRAUTO.md` | Update to reflect current scaffold structure and prauto behavior. May include moderate detail (file trees, config keys, workflow steps). |
| 5–6 | `feature/*.md`, `feature/spoke/*.md` | Full detail allowed. These are the deep-dive specs. |

### Brevity rules

- **`CLAUDE.md`** (root): Keep as brief as possible. Orientation document, not a reference. Only names, key commands, and pointers to detailed specs. Remove anything that belongs in `AI_SCAFFOLD.md` or feature specs.
- **`README.md`** files: Quick-start focused. Prerequisites, install command, access details, link to spec. No architecture discussion.
- **SKILL.md declarations**: `description` should be one sentence. `argument-hint` should match actual accepted arguments.
- **High-level specs (priority 1–3)**: Describe *what* and *why*, not *how*. Implementation details belong in priority 4–6 documents or in impl files.
- **Generally, all spec-side documents should stay brief.** They exist to orient and constrain, not to replicate implementation details. 
- In spec, focus on architecture, decisions, and constraints. From spec, remove verbatim template code, full code blocks, and script snippets that duplicate the impl files.

### Change direction heuristic

- If **impl is more recent** (has features/options that spec doesn't mention) → propose updating spec to document the current state.
- If **spec has requirements** that impl doesn't fulfill → flag as "spec gap in implementation" — do not auto-modify impl code (too risky without tests). Report it for the user to decide.
- If **both have drifted** → propose spec update to match impl reality, and list impl gaps separately.

---

## Step 4 — Present findings and apply changes

### Report format

Present a structured summary before making changes:

```
## Sync Report — <scope(s)>

### Discrepancies Found

#### <Scope Name>

| # | File | Side | Issue | Proposed Action |
|---|------|------|-------|-----------------|
| 1 | spec/AI_PRAUTO.md | spec | Missing reference to new lib module | Add to file tree |
| 2 | .prauto/README.md | spec | Stale prerequisites list | Update to match reality |
| 3 | .claude/skills/dev-env/SKILL.md | spec (decl) | argument-hint missing new action | Update frontmatter |
| ... | ... | ... | ... | ... |

#### Spec Gaps in Implementation (not auto-fixed)
- spec/feature/API.md requires endpoint X, but src/api/routers/ doesn't implement it

### High-Level Spec Impact
- CLAUDE.md: <no change needed | brief update proposed>
- ARCHITECTURE.md: <no change needed | update proposed>
```

### Apply changes

1. **Ask the user** to confirm before applying. Show the proposed edits clearly.
2. Apply edits using the `Edit` tool (prefer surgical edits over full rewrites).
3. For high-level specs and `CLAUDE.md`, show the diff preview first — these files affect all agents.
4. After applying, re-read modified files to verify consistency.

---

## Cross-scope consistency check

When `all` is selected (or multiple scopes), perform an additional pass after the per-scope steps above:

- Flag any **contradictions between priority levels** (e.g., a feature spec contradicting ARCHITECTURE.md, or a skill declaration contradicting its parent spec).
- The spec hierarchy in §Step 3 determines which document is authoritative when there is a conflict — higher priority wins.
