---
name: spec-sync-with-impl
description: >-
  Bidirectional sync between DataSpoke specs and implementation. Accepts a
  preset scope (prauto, ai-scaffold, k8s-deploy, helm-charts, api, ref, backend,
  frontend, all) or a free-form description of an area (e.g., "recently
  developed backend ingestion secret resolution"). Audits the scope, reports
  the gaps, asks the user which direction to resolve each gap (spec→impl,
  impl→spec, or leave-as-flagged), then applies the chosen edits. Trigger when
  specs and code may have drifted apart and the user wants them reconciled.
argument-hint: '[preset-scope | "free-form description of an area"]'
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
- `helm-charts/` chart definitions, values, templates, `bin/` install/uninstall/build scripts, and `peripherals/` manifests
- `src/api/` routers, schemas, middleware (FastAPI implementation as API SSOT)
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

Scope can be one of the **preset scopes** below, OR a **free-form description** of any area in the repo (e.g., "recently developed backend ingestion secret resolution function", "Airflow scheduler retry policy", "frontend dataset detail page"). The skill is not restricted to the preset list — presets are shortcuts for common areas, free-form lets the user target anything.

Parse `$ARGUMENTS`:

- If `$ARGUMENTS` matches a preset keyword (or comma-separated list of them), use the preset mapping table below.
- If `$ARGUMENTS` is free-form text, treat it as a scope description and follow the **Free-form scope resolution** flow.
- If `$ARGUMENTS` is empty or ambiguous, output the menu below and **wait for the user to reply**.

```
Select scope(s) to sync. You can pick presets, free-form, or a mix:

Presets (numbers or keywords, comma-separated):
  1. all          — All preset scopes that have both spec and impl files
  2. prauto       — .prauto scripts, prauto-related specs and skills
  3. ai-scaffold  — CLAUDE.md, .claude/ settings, hooks, agents, all skills
  4. k8s-deploy   — helm-charts/bin scripts, k8s-deploy skill
  5. helm-charts  — Helm chart definitions, helm-charts/dev-peripherals/, HELM_CHART spec
  6. api          — API specs, src/api/ code
  7. ref          — ref/ setup scripts and reference materials
  8. backend      — Backend services, shared libs, Airflow workflows
  9. frontend     — Next.js frontend code

Or describe a free-form scope in plain text, e.g.:
  - "recently developed backend ingestion secret resolution"
  - "Airflow scheduler retry policy"
  - "/api/v1/spoke/governance routes only"

Examples: "1"   "prauto, api"   "2,3,6"   "frontend dataset detail page"
```

Parse the user's reply: split on commas; each item is either a preset (number/keyword) or a free-form description. The directories listed in the preset table are **starting points** — always glob the actual tree to discover what exists. If files have moved or been renamed, follow the real structure rather than these hints.

### Preset scope mapping

| Scope keyword | Spec side | Impl side |
|---------------|-----------|-----------|
| `prauto` | `spec/AI_PRAUTO.md`, prauto-related skill declarations, `.prauto/README.md` | `.prauto/` scripts and libs, prauto-related skill logic bodies |
| `ai-scaffold` | `spec/AI_SCAFFOLD.md`, `AGENTS.md`, `CLAUDE.md`, all skill declarations across `.agents/skills/` | `scaffold/` (roles/memory/bin), `.claude/` settings/hooks/agents, all skill logic bodies |
| `k8s-deploy` | `spec/feature/HELM_CHART.md` (bin/ + workflow sections), k8s-deploy skill declaration, `helm-charts/README.md` | `helm-charts/bin/` scripts, k8s-deploy skill logic body |
| `helm-charts` | `spec/feature/HELM_CHART.md` | `helm-charts/dataspoke/`, `helm-charts/dev-peripherals/` (incl. `langfuse/`) charts/values/templates/manifests |
| `api` | `spec/API.md`, `spec/API_DESIGN_PRINCIPLE_en.md`, `src/api/README.md` | `src/api/` routers/schemas/auth/middleware |
| `ref` | `spec/AI_SCAFFOLD.md` (ref section), ref-setup skill declaration, `ref/README.md` | `ref/` setup scripts and reference materials, ref-setup skill logic body |
| `backend` | `spec/feature/BACKEND.md`, `BACKEND_LLM.md`, `BACKEND_SCHEMA.md`, `AUTH.md`, `VALIDATION.md`, `SECRET_RESOLUTION.md` | `src/backend/` per-feature services, `src/shared/` (config/db/llm/datahub/models/secrets/vector/graph), `src/workflows/` Airflow DAGs + params |
| `frontend` | `spec/feature/FRONTEND_BASIC.md` + `FRONTEND_{GOVERNANCE,INGESTION,VALIDATION,ONTOGEN,METAGEN}.md`, `src/frontend/README.md` | `src/frontend/` app routes, components, lib, types |
| `all` | All of the above | All of the above |

- If the user selects `all`, expand to every preset scope that has both spec and impl files present.

### Free-form scope resolution

When the scope is free-form text:

1. **Extract anchor terms** from the description — feature names, function/file/symbol names, route fragments, component names, time hints (e.g. "recently developed"). Combine with synonyms the user might be using loosely.
2. **Discover candidate files** by searching both sides:
   - Spec side: `Glob` `spec/**/*.md`, `**/README.md`, `CLAUDE.md`, `.agents/skills/*/SKILL.md` (frontmatter only). `Grep` for anchor terms across these.
   - Impl side: `Glob` likely directories (`src/**`, `.prauto/**`, `helm-charts/**`, `ref/**`, SKILL.md logic bodies). `Grep` for anchor terms.
   - For "recently developed" or similar time hints, also use `git log` / recently-touched files as a signal (read recent commits with `git log --since=<sensible-window> --name-only` via the user's tools if needed; otherwise rely on filename/Grep matches).
3. **Show a candidate file list to the user before reading deeply**:
   ```
   Free-form scope: "<user's description>"

   Spec-side candidates:
     - spec/feature/INGESTION.md
     - src/backend/ingestion/README.md

   Impl-side candidates:
     - src/backend/ingestion/secret_resolver.py
     - src/backend/ingestion/auth.py
     - tests/integration/test_secret_resolution.py
     - helm-charts/dataspoke/templates/secret-vault.yaml

   Reply: "ok" to proceed, or edit the list (add/remove paths).
   ```
4. **Wait for confirmation** before moving to Step 1. The user may add or drop files. Respect their final list as the working scope.
5. If no candidates surface on one side, report the asymmetry — that itself is a gap (e.g., "impl exists for X but spec is silent" or vice versa) — and ask whether to proceed anyway.

---

## Step 1 — Collect inventory

For each selected scope:

1. **Preset scope**: `Glob` for all spec-side and impl-side files using the areas in the preset mapping table.
2. **Free-form scope**: use the file list confirmed in §Free-form scope resolution as the inventory; no further globbing needed.
3. Build a file inventory: `{scope, side, path, exists}`.
4. If a scope has no spec files or no impl files, report it. For preset scopes, skip with a warning. For free-form scopes, treat the missing side as a major gap to surface in Step 2 (an entire side being absent is itself the most important finding).

---

## Step 2 — Read and assess gaps

For each scope, read files on both sides and identify discrepancies. Do **not** assume a sync direction yet — at this step you are only enumerating gaps.

### What to look for

| Category | Description | Example |
|----------|-------------|---------|
| **Structural drift** | Spec lists components/scripts that don't exist in impl, or impl has components not mentioned in spec | Spec describes `lib/foo.sh`, file is missing |
| **Naming mismatch** | Spec uses one name for a concept; impl uses a different name | Spec calls it `sync-registry`, impl exposes `datahub-sync` |
| **Behavioral drift** | Spec describes a workflow or protocol; impl does it differently | Spec says retry 3×, impl retries 5× |
| **Undocumented in spec** | Impl has features/options not documented in spec or README | New CLI flag, new module, new env var |
| **Stale references** | Spec references files, paths, or versions that have changed | `python 3.11` in spec, runtime is `3.13` |
| **Skill declaration drift** | SKILL.md frontmatter/routing doesn't match the skill's logic body | `argument-hint` lists actions the body never handles |

### Reading strategy

- **Spec side**: Read fully — these define the intended behavior.
- **Impl side**: Focus on structure, exported functions/endpoints, CLI options, and high-level flow. Don't read every line of business logic unless the spec describes that level of detail.
- **SKILL.md**: Read the declaration (frontmatter + routing) as spec; skim the logic body for workflow steps and behavioral contracts; compare the two sides against each other.

---

## Step 3 — Classify each gap with proposed direction

For every gap, label it with one of three direction options. Always show the user the proposed default; the user can override per-gap or globally in Step 4.

| Gap shape | Direction options | Default proposal |
|-----------|-------------------|------------------|
| Spec describes X, impl does not have X | (a) implement X (spec→impl) · (b) remove X from spec (impl→spec) · (c) leave flagged | Default to **(c) leave flagged** if X looks like an unimplemented requirement; **(b)** if X looks dropped/obsolete. Never silently default to (a). |
| Impl has Y, spec does not describe Y | (a) document Y in spec (impl→spec) · (b) remove Y from impl (spec→impl) · (c) leave flagged | Default to **(a) document** for additive features that are clearly working; **(c) leave flagged** for things that look accidental or out of scope. |
| Spec says A, impl does B (contradiction) | (a) update impl to A (spec→impl) · (b) update spec to B (impl→spec) · (c) leave flagged | Default to **(b) update spec** when impl is well-established and the difference is a refinement; **(a) update impl** when spec is a higher-priority document (see hierarchy below); **(c)** when unclear. |

### Spec hierarchy awareness

When choosing defaults, respect the priority hierarchy from `CLAUDE.md`. Higher-priority documents constrain lower ones.

| Priority | Documents | Sync rule |
|----------|-----------|-----------|
| 1 | `MANIFESTO_en/kr.md`, `API.md`, `USE_CASE_en/kr.md` | **Never modify the spec.** A contradiction must be resolved by changing impl, or by escalating to the user. |
| 2 | `API_DESIGN_PRINCIPLE_en/kr.md`, `DATAHUB_INTEGRATION.md` | Fix factual inaccuracies only. Conventions stay abstract — no impl details bleed in. |
| 3 | `ARCHITECTURE.md`, `TESTING.md` | Update component lists, tech stack, data flows when they've changed. Architectural level only. |
| 4 | `AI_SCAFFOLD.md`, `AI_PRAUTO.md` | Update to reflect current scaffold/prauto behavior. May include moderate detail (file trees, config keys, workflow steps). |
| 5 | `feature/*.md` | Full detail allowed. These are the deep-dive specs. |

### Brevity rules (apply when writing into spec side)

- **`CLAUDE.md`** (root): Keep brief. Orientation only — names, key commands, pointers to detailed specs.
- **`README.md`** files: Quick-start focused. Prerequisites, install command, access details, link to spec.
- **SKILL.md declarations**: `description` should be one sentence. `argument-hint` should match accepted arguments.
- **High-level specs (priority 1–3)**: Describe *what* and *why*, not *how*.
- Strip verbatim code blocks and script snippets from spec — those belong in the impl files.

### Impl-side guard rail (apply when writing into impl side)

When the chosen direction is spec→impl, decide whether the change is light or substantial:

- **Light** (this skill may apply directly): renaming a CLI flag, fixing a hardcoded value, updating a SKILL.md logic body, removing a dead reference, updating a version string.
- **Substantial** (this skill must NOT write code directly): introducing a new API endpoint, DB table/column, pgvector collection, Airflow DAG, or any cross-layer feature. Per `CLAUDE.md` Implementation Workflow, route these through the proper plan → generator agent → reviewer flow. In the report, mark them as "needs implementation workflow" and surface them to the user instead of editing.

---

## Step 4 — Present findings and ask for direction

Present a structured report **before making any changes**:

```
## Sync Report — <scope(s)>

### Gaps Found

#### <Scope Name>

| # | Side(s) involved | Gap shape | Summary | Proposed default | Options |
|---|------------------|-----------|---------|------------------|---------|
| 1 | spec/AI_PRAUTO.md ↔ .prauto/lib/ | undocumented in spec | new module `quota.sh` not mentioned | impl→spec (document) | a) document  b) remove from impl  c) leave flagged |
| 2 | .prauto/README.md ↔ .prauto/heartbeat.sh | stale reference | prerequisites list outdated | impl→spec (update spec) | a) update spec  b) update impl  c) leave flagged |
| 3 | .agents/skills/k8s-deploy/SKILL.md decl ↔ body | skill declaration drift | argument-hint missing `reset` action | impl→spec (decl update) | a) decl→match body  b) body→match decl  c) leave flagged |
| 4 | spec/API.md ↔ src/api/routers/ | spec gap in impl (substantial) | endpoint X required, not implemented | leave flagged → implementation workflow | a) plan+generate  b) drop from spec  c) leave flagged |

### High-Level Spec Impact
- CLAUDE.md: <no change needed | brief update proposed>
- ARCHITECTURE.md: <no change needed | update proposed>
```

### User prompt

After the report, ask the user how to proceed. Offer these answer shapes:

- `accept defaults` — apply every proposed default.
- `accept defaults except 3,7` — apply defaults but leave specified items flagged.
- `1=a, 2=b, 3=c, ...` — per-item override.
- `direction=impl→spec` or `direction=spec→impl` — globally bias all defaults one way (still skips substantial spec→impl per the guard rail).
- `leave all flagged` — report only, no edits.

Wait for the user's reply. Do not apply changes until they answer.

---

## Step 5 — Apply changes

1. Group decisions by side (spec edits, impl edits, flagged-only).
2. **For each spec edit**: prefer surgical `Edit` over full rewrites. For high-level specs (priority 1–3) and `CLAUDE.md`, show the diff preview to the user one more time before writing.
3. **For each light impl edit**: apply with `Edit`.
4. **For each substantial impl change**: do not edit. Output a clearly labeled "needs implementation workflow" section listing the gaps, the relevant spec excerpts, and the recommended generator agent (`backend`, `airflow-dag`, `frontend`, `test`, `k8s-helm`). Suggest the user start with `/spec-write` if the spec itself needs sharpening before generation.
5. Re-read modified files to verify consistency. Report which gaps were closed, which were left flagged, and which were routed to the implementation workflow.

---

## Cross-scope consistency check

When `all` is selected (or multiple scopes), perform an additional pass after the per-scope steps:

- Flag contradictions between priority levels (e.g., a feature spec contradicting `ARCHITECTURE.md`, or a skill declaration contradicting its parent spec).
- Higher priority wins. If resolving the contradiction would require modifying a priority-1 document, do not edit — escalate to the user.
