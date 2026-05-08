---
name: spec-write
description: Author timeless specification documents under spec/ (top-level or spec/feature/<FEATURE>.md) following the project hierarchy, naming conventions, and templates. Use when the user asks to write, design, or document a DataSpoke feature, component, or architectural decision as a reference spec. Do NOT use for implementation plans — those live in GitHub issues/PRs and are produced by native Plan mode or PRauto.
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
├── USE_CASE_en.md / _kr.md    ← Conceptual scenarios (UC1–UC5) by user group.
├── AI_SCAFFOLD.md             ← Goal 2: Claude Code scaffold conventions.
├── TESTING.md                 ← Testing conventions: toolchain, unit/integration/E2E
│                                 workflows, Imazon test data.
├── DATAHUB_INTEGRATION.md     ← DataHub SDK patterns, aspect catalog, GraphQL,
│                                 event subscription, error handling.
├── API_DESIGN_PRINCIPLE_en.md / _kr.md
│                              ← REST API conventions: URI structure, request/response
│                                 format, content/metadata separation, meta-classifiers.
│
└── feature/            ← Deep-dive specs for ALL FIVE BASELINE features
    │                     (Ingestion Control, Validation, Ontology Generation,
    │                     Metadata Generation, Governance) plus cross-cutting
    │                     infrastructure and user-group-specific FRONTEND
    │                     specs. Timeless reference format.
    └── <FEATURE>.md      e.g. BACKEND.md, BACKEND_SCHEMA.md,
                          FRONTEND_BASIC/DE/DA/DG.md, DEV_ENV.md, HELM_CHART.md
```

**Routing rules:**
- Top-level `spec/` — project-wide documents only. Do NOT create new top-level files unless the topic affects the whole system and warrants an architectural-level document.
- `spec/feature/` — **all baseline feature deep-dives** (Ingestion Control, Validation, Ontology Generation, Metadata Generation, Governance) plus cross-cutting infrastructure (e.g. `API.md`, `DEV_ENV.md`, `BACKEND.md`, `HELM_CHART.md`) and user-group-specific FRONTEND specs (`FRONTEND_DE/DA/DG.md`). The user-group framing (DE / DA / DG) is a UI / API extensibility surface only — baseline governance routes under `/spoke/dg/` are still part of the baseline product and live here. Organization-specific extensions in forks add new files alongside (e.g. `feature/SEARCH_DA.md`); no separate subdirectory is required.

Implementation plans and decision records are tracked via GitHub Issues and PRs, not in the spec directory.

---

## Step 1 — Read context

Always read these before writing:
- `spec/MANIFESTO_en.md` — canonical baseline-feature taxonomy and naming (highest authority)
- `spec/ARCHITECTURE.md` — components, tech stack, data flows, feature-to-architecture mapping, shared services (Ontology Generator, DataHub Client Wrapper), deployment

Additionally, read these when relevant to the topic:
- `spec/DATAHUB_INTEGRATION.md` — when the feature involves DataHub read/write/event patterns
- `spec/API_DESIGN_PRINCIPLE_en.md` — when designing API endpoints
- `spec/USE_CASE_en.md` — for use case context (UC1–UC5)

If writing about a specific feature, also check for an existing `spec/feature/<FEATURE>.md` to extend rather than create a duplicate.

---

## Step 2 — Determine destination and document type from `$ARGUMENTS`

| Destination | When to use | Document type |
|-------------|-------------|---------------|
| `spec/feature/<FEATURE>.md` | A baseline feature (Ingestion Control, Validation, Ontology Generation, Metadata Generation, Governance), a common cross-cutting topic (API, dev environment, shared services), a user-group-specific FRONTEND spec (`FRONTEND_DE/DA/DG.md`), or an organization-specific extension in a fork (e.g. `SEARCH_DA.md`). | Feature Spec (see template A; tag with user group when applicable) |
| `spec/<DOC>.md` (top-level) | Only for project-wide topics that belong alongside MANIFESTO and ARCHITECTURE | Top-level spec (use template A without feature context) |

---

## Step 3 — Write the document

Use the template for the chosen destination. Follow these style rules for both:
- H1 title
- H2 section headings, H3 sub-headings
- ASCII diagrams for component/flow illustrations
- Tables for comparisons and field definitions
- Code blocks for schemas, interfaces, API examples
- User group names must match the MANIFESTO exactly: **DE** (Data Engineering), **DA** (Data Analysis), **DG** (Data Governance) — these are UI / API extensibility surfaces, not feature partitions
- Baseline feature names must match the MANIFESTO §2.1 exactly: **Ingestion Control**, **Validation**, **Ontology Generation**, **Metadata Generation**, **Governance**
- Product name is always `DataSpoke` (no space)
- API URIs follow the three-tier pattern: `/api/v1/spoke/common/…` (shared), `/api/v1/spoke/[de|da|dg]/…` (user-group), `/api/v1/hub/…` (DataHub pass-through)
- For DataHub integration details, reference `DATAHUB_INTEGRATION.md` rather than duplicating SDK patterns or aspect catalogs
- For API convention details, reference `API_DESIGN_PRINCIPLE_en.md` rather than restating URI/response format rules
- When referencing architecture-level concepts (shared services, data flows, tech stack), align with `ARCHITECTURE.md`

---

## Template A — Feature Spec (`spec/feature/<FEATURE>.md`)

No version/date/author metadata block. This is a timeless reference document.

For user-group-specific or organization-extension features, include the user group tag. For common features, omit it.

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

- If a new `spec/feature/` document introduces components or data models that belong in the architecture overview, update `spec/ARCHITECTURE.md`.
- If the document changes how a use case is realized, note it in `spec/USE_CASE_en.md` as a cross-reference.
- Never modify `spec/MANIFESTO_en.md` or `spec/MANIFESTO_kr.md`.
