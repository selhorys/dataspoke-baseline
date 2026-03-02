---
name: plan-doc
description: Write specification or planning documents in spec/ following the project hierarchy. Use when the user asks to design, spec out, or document a DataSpoke feature, component, architectural decision, or implementation plan.
argument-hint: <topic>
allowed-tools: Read, Write, Edit
---

## Spec Directory Hierarchy

```
spec/
├── MANIFESTO_en.md / _kr.md   ← Highest authority. Product identity, user-group
│                                 taxonomy (DE/DA/DG), naming. Never modify.
├── ARCHITECTURE.md            ← System-wide architecture: components, tech stack,
│                                 data flows, feature-to-architecture mapping,
│                                 shared services, deployment. Conforms to MANIFESTO.
├── USE_CASE_en.md / _kr.md    ← Conceptual scenarios (UC1–UC8) by user group.
├── AI_SCAFFOLD.md             ← Goal 2: Claude Code scaffold conventions.
├── TESTING.md                 ← Testing conventions: toolchain, unit/integration/E2E
│                                 workflows, Imazon test data.
├── DATAHUB_INTEGRATION.md     ← DataHub SDK patterns, aspect catalog, GraphQL,
│                                 event subscription, error handling.
├── API_DESIGN_PRINCIPLE_en.md / _kr.md
│                              ← REST API conventions: URI structure, request/response
│                                 format, content/metadata separation, meta-classifiers.
│
├── feature/            ← Deep-dive specs for COMMON (cross-cutting) features.
│   │                     Clean, timeless reference format.
│   │                     No log-style content (no dates, authors, changelogs).
│   └── <FEATURE>.md      e.g. API.md, DEV_ENV.md
│
└── feature/spoke/      ← Deep-dive specs for USER-GROUP-SPECIFIC features.
    │                     Same timeless reference format as feature/.
    │                     One file per feature, tagged by user group (DE/DA/DG).
    └── <FEATURE>.md      e.g. INGESTION.md, NL_SEARCH.md, METRICS_DASHBOARD.md
```

**Routing rules:**
- Top-level `spec/` — project-wide documents only. Do NOT create new top-level files unless the topic affects the whole system and warrants an architectural-level document.
- `spec/feature/` — common/cross-cutting feature deep-dives that are not specific to a single user group (e.g. `API.md`, `DEV_ENV.md`, shared infrastructure).
- `spec/feature/spoke/` — user-group-specific feature deep-dives. These map to features defined in the MANIFESTO under DE, DA, or DG groups. Examples: `INGESTION.md` (DE), `ONLINE_VALIDATOR.md` (DE/DA), `NL_SEARCH.md` (DA), `METRICS_DASHBOARD.md` (DG).

Implementation plans and decision records are tracked via GitHub Issues and PRs, not in the spec directory.

**How to decide between `feature/` and `feature/spoke/`:**
- If the feature belongs to a specific user group in the MANIFESTO (DE/DA/DG) → `feature/spoke/`
- If the feature is cross-cutting infrastructure, shared across groups, or not user-group-specific → `feature/`
- When in doubt, check the MANIFESTO's "Features by User Group" section

---

## Step 1 — Read context

Always read these before writing:
- `spec/MANIFESTO_en.md` — canonical user-group taxonomy and naming (highest authority)
- `spec/ARCHITECTURE.md` — components, tech stack, data flows, feature-to-architecture mapping, shared services (Ontology Builder, Quality Score Engine), deployment

Additionally, read these when relevant to the topic:
- `spec/DATAHUB_INTEGRATION.md` — when the feature involves DataHub read/write/event patterns
- `spec/API_DESIGN_PRINCIPLE_en.md` — when designing API endpoints
- `spec/USE_CASE_en.md` — for use case context (UC1–UC8)

If writing about a specific feature, also check for an existing `spec/feature/<FEATURE>.md` or `spec/feature/spoke/<FEATURE>.md` to extend rather than create a duplicate.

---

## Step 2 — Determine destination and document type from `$ARGUMENTS`

| Destination | When to use | Document type |
|-------------|-------------|---------------|
| `spec/feature/spoke/<FEATURE>.md` | User-group-specific feature from the MANIFESTO: DE (Ingestion, Online Validator, Doc Generation), DA (NL Search, Text-to-SQL Metadata, Validator), DG (Metrics Dashboard, Multi-Perspective Overview) | Spoke Feature Spec (see template A) |
| `spec/feature/<FEATURE>.md` | Common/cross-cutting feature not specific to one user group (API design, dev environment, shared services) | Common Feature Spec (see template A, without user-group context) |
| `spec/<DOC>.md` (top-level) | Only for project-wide topics that belong alongside MANIFESTO and ARCHITECTURE | Top-level spec (use template A without feature context) |

---

## Step 3 — Write the document

Use the template for the chosen destination. Follow these style rules for both:
- H1 title
- H2 section headings, H3 sub-headings
- ASCII diagrams for component/flow illustrations
- Tables for comparisons and field definitions
- Code blocks for schemas, interfaces, API examples
- User group names must match the MANIFESTO exactly: **DE** (Data Engineering), **DA** (Data Analysis), **DG** (Data Governance)
- Feature names must match the MANIFESTO: **Deep Technical Spec Ingestion**, **Online Data Validator**, **Automated Documentation Generation**, **Natural Language Search**, **Text-to-SQL Optimized Metadata**, **Enterprise Metrics Time-Series Monitoring**, **Multi-Perspective Data Overview**
- Product name is always `DataSpoke` (no space)
- API URIs follow the three-tier pattern: `/api/v1/spoke/common/…` (shared), `/api/v1/spoke/[de|da|dg]/…` (user-group), `/api/v1/hub/…` (DataHub pass-through)
- For DataHub integration details, reference `DATAHUB_INTEGRATION.md` rather than duplicating SDK patterns or aspect catalogs
- For API convention details, reference `API_DESIGN_PRINCIPLE_en.md` rather than restating URI/response format rules
- When referencing architecture-level concepts (shared services, data flows, tech stack), align with `ARCHITECTURE.md`

---

## Template A — Feature Spec (`spec/feature/spoke/<FEATURE>.md` or `spec/feature/<FEATURE>.md`)

No version/date/author metadata block. This is a timeless reference document.

For spoke features, include the user group tag. For common features, omit it.

```markdown
# <Feature Name>

> **User Group**: <DE | DA | DG | DE/DA (shared)>
> (omit this line for common features in spec/feature/)

## Table of Contents
1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Design](#design)
4. [Data Model](#data-model)
5. [Interfaces](#interfaces)
6. [Open Questions](#open-questions)

## Overview

## Goals & Non-Goals
### Goals
### Non-Goals

## Design

## Data Model

## Interfaces

## Open Questions
- [ ]
```

---

## Step 4 — Update cross-references if needed

- If a new `spec/feature/` or `spec/feature/spoke/` document introduces components or data models that belong in the architecture overview, update `spec/ARCHITECTURE.md`.
- If the document changes how a use case is realized, note it in `spec/USE_CASE_en.md` as a cross-reference.
- Never modify `spec/MANIFESTO_en.md` or `spec/MANIFESTO_kr.md`.
