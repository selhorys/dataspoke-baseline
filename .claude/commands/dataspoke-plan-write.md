Guide the user through interactive spec authoring. Use the `plan-doc` skill as the writing engine — this command adds orchestration: scope selection, iterative Q&A, writing plan review, document writing, and AI scaffold recommendations.

## Step 1 — Survey existing specs

Before asking the user anything, build an inventory of the spec landscape:

1. Read `spec/MANIFESTO_en.md` — extract user-group taxonomy (DE/DA/DG), feature names, and naming conventions.
2. Read `spec/ARCHITECTURE.md` — extract components, feature-to-architecture mapping (UC1–UC8), shared services, tech stack.
3. Glob `spec/*.md`, `spec/feature/*.md`, `spec/feature/spoke/*.md` to build a list of all existing spec documents.
4. Store this inventory internally. Use it to:
   - Avoid asking questions whose answers are already documented
   - Identify cross-reference opportunities
   - Detect potential overlaps with existing specs

Do NOT present the inventory to the user — it is context for the Q&A and planning steps.

---

## Step 2 — Ask scope

Use `AskUserQuestion` to present 4 scope options:

| Option | Label | Description |
|--------|-------|-------------|
| 1 | **Architectural guideline** | Top-level `spec/` document for system-wide concerns |
| 2 | **Common feature spec** | Cross-cutting feature in `spec/feature/` |
| 3 | **User-group-specific feature spec** | DE/DA/DG feature in `spec/feature/spoke/` |

Store the selected scope for subsequent steps.

---

## Step 3 — Gather context through iterative Q&A

Start with a common first question for all scopes: **"What is the purpose and motivation for this document?"**

Then ask scope-specific follow-up questions. Use `AskUserQuestion` for each round.

### Scope-specific questions and convergence criteria

**Scope 1 — Architectural guideline:**
- Which system-wide concern does this address?
- Which components from ARCHITECTURE.md are affected?
- Does this introduce new shared services?
- **Converge when**: system-wide impact is clear, relationship to existing arch docs established, affected components identified.

**Scope 2 — Common feature spec:**
- Confirm the feature is cross-cutting (not user-group-specific). If it is group-specific, suggest switching to scope 3.
- Does the feature involve DataHub integration? If yes, which patterns (read/write/event)?
- Does the feature expose API endpoints? If yes, which user groups consume them?
- **Converge when**: feature purpose clear, components touched identified, integration points known.

**Scope 3 — User-group-specific feature spec:**
- **Which user group?** (DE / DA / DG / shared DE+DA, etc.)
- Which MANIFESTO feature does this implement or extend?
- Which use cases (UC1–UC8) does it address?
- What DataHub read/write/event patterns are needed?
- What API surface is anticipated? (`/api/v1/spoke/common/…`, `/api/v1/spoke/[de|da|dg]/…`, or `/api/v1/hub/…`)
- **Converge when**: user group confirmed, MANIFESTO feature mapping established, DataHub needs identified, API surface outlined.

### Convergence rules

- After each Q&A round, check whether the criteria for the selected scope are met.
- If the user's initial description is rich enough to satisfy all criteria, skip further questions. Avoid Q&A fatigue.
- Maximum 4 Q&A rounds before proceeding (the writing plan step provides another review opportunity).

---

## Step 4 — Review writing plan

Before writing, present a structured plan to the user:

1. **File(s) to create or modify** — full paths, whether new or update
2. **Document type and template** — Template A (feature spec) from `plan-doc`
3. **Content outline** — H2 sections with 1–2 sentence descriptions of what each section will cover
4. **Cross-references to update** — other spec files that should reference this document (e.g., ARCHITECTURE.md, USE_CASE_en.md)
5. **MANIFESTO compliance notes** — confirm naming, user-group taxonomy, and product identity alignment

Present this as a formatted summary. Ask the user to confirm or provide corrections.

If the user requests substantial changes, re-present the updated plan. Minor corrections can be incorporated directly.

---

## Step 5 — Write the spec

Delegate to `plan-doc` skill conventions:

1. **Read context** (plan-doc Step 1): Read `spec/MANIFESTO_en.md`, `spec/ARCHITECTURE.md`, and any topic-relevant docs (DATAHUB_INTEGRATION.md, API_DESIGN_PRINCIPLE_en.md, USE_CASE_en.md) as appropriate.
2. **Write using the correct template** (plan-doc Step 3):
   - Use Template A (feature spec format, timeless reference)
   - Apply all plan-doc style rules: H1 title, H2/H3 headings, ASCII diagrams, tables for comparisons, code blocks for schemas/APIs, MANIFESTO-compliant naming.
3. **Update cross-references** (plan-doc Step 4): If the new document introduces components or data models that belong in the architecture overview, update `spec/ARCHITECTURE.md`. If it changes use case realization, note it in `spec/USE_CASE_en.md`. Never modify MANIFESTO files.

---

## Step 6 — Print task summary

After writing, present a concise summary:

- **Files created** — full paths
- **Files modified** — full paths with brief description of what changed
- **Document type** — which template was used (A or B)
- **Template** — Template A (feature spec)
- **Key content** — 3–5 bullet points summarizing the document's main contributions
- **Cross-references added** — which existing docs were updated and how

---

## Step 7 — AI scaffold recommendation

Evaluate whether `.claude/` configuration changes would help support the newly documented feature or plan. Consider:

- **New agent definitions** (`.claude/agents/`) — if the document introduces a new component area that would benefit from a specialized subagent
- **Skill updates** (`.claude/skills/`) — if new workflow patterns emerged that should be codified
- **Command updates** (`.claude/commands/`) — if new operational workflows are needed
- **Permission updates** (`.claude/settings.json`) — if new tools or cluster operations are anticipated
- **`CLAUDE.md` updates** — if the spec hierarchy description needs adjustment

Present recommendations as a bulleted list. Explain the rationale for each recommendation. **Do not auto-execute any changes** — these are suggestions for the user to review and approve in a follow-up session.

If no scaffold changes are warranted, say so explicitly.
