# E2E Route Coverage Map

Maps every route under `src/frontend/app/` to its covering E2E test(s).
Two groups cover the full surface: **use-case** (mirrors api-wired UC tests) and
**ground** (mirrors spot integration tests — narrow per-page flows).

`spec: spec/TESTING.md §End-to-End (E2E) Testing — use-case + ground groups together
cover every route; this map is the acceptance artifact for "fully covered".`

Every route below is covered by at least one group. A `—` in a column means that
group does not cover the route; the other column does.

---

## Legend

- **UC test**: `tests/e2e/use-case/<file>.spec.ts` covering this route
- **Ground test**: `tests/e2e/ground/<feature>/<file>.spec.ts` covering this route

---

## Routes

### Public routes

| Route | UC test | Ground test |
|---|---|---|
| `/login` | `_smoke.spec.ts` (auth harness) | `ground/auth/login.public.spec.ts` (form render, bad-creds error, Register link, Forgot-password link) |
| `/register` | — | `ground/auth/register.public.spec.ts` (form render, short-password validation, real signup → redirect) |
| `/forgot-password` | — | `ground/auth/password-reset.public.spec.ts` (form render, submit → confirmation state) |
| `/reset-password` | — | `ground/auth/password-reset.public.spec.ts` (no-token error state, dummy-token form render, short-password validation) |

### App routes (authenticated)

#### Per-dataset hub (shared)

The unified per-dataset page merges the formerly separate `/ingestion/data`,
`/validation/data`, and `/metagen/data` surfaces — three summary cards
(Ingestion / Validation / MetaGen) + four foldable panels (Ingestion, Validation,
MetaGen, Events). The retired per-feature `/{feature}/data/[urn]` routes are
redirects to `/data/[urn]`.

| Route | UC test | Ground test |
|---|---|---|
| `/data/[urn]` | `uc1-02-active-custom-postgres.spec.ts` step 6 (Ingestion panel reverse-lookup); `uc2-01-validation.spec.ts` steps 2, 4–7 (Validation panel: conf, charts, freeze/restore Undelete gated by the page-level "Show deleted" toggle — default OFF reads removed slot as Create empty-state, ON reveals the frozen Undelete view; validation events in unified Events panel); `uc4-01-metadata-generation.spec.ts` steps 3, 7, 8, 9 (MetaGen panel boundary form, candidate review, metagen events in unified Events panel) | `ground/data/hub.spec.ts` (URN header + 3 summary cards; 4 foldable panels fold/unfold; Events major-type filter all-checked default + uncheck narrows) |

#### Ingestion (UC1)

| Route | UC test | Ground test |
|---|---|---|
| `/ingestion` (redirects to `/ingestion/conf`) | — | — |
| `/ingestion/conf` | `uc1-02-active-custom-postgres.spec.ts` step 7; `uc1-03-passive-kafka.spec.ts` step 6; `uc1-01-datahub-managed.spec.ts` step 3 (list view, DataHub-managed filter; wrappers hidden) | `ground/shell/reader-write-suppressed.reader.spec.ts` (Reader read-only: "Create source" absent) |
| `/ingestion/sources/new` | `uc1-02-active-custom-postgres.spec.ts` step 1 (create ACTIVE_CUSTOM_MANAGED); `uc1-03-passive-kafka.spec.ts` step 1 (create PASSIVE) | — |
| `/ingestion/sources/[id]` | `uc1-02-active-custom-postgres.spec.ts` steps 2–7; `uc1-03-passive-kafka.spec.ts` steps 2–3, 5–6; `uc1-01-datahub-managed.spec.ts` steps 4–6 (detail, run, datasets, events, read-only, wrapper-tagged INGESTION.COMPLETE event row, pipeline_name/high authority cell after real execution) | — |
| `/ingestion/unmanaged` | `uc1-03-passive-kafka.spec.ts` steps 0, 4 (before/after source creation) | — |
| `/ingestion/data/[urn]` (redirects to `/data/[urn]`) | covered via `/data/[urn]` (see Per-dataset hub) | — |

#### Governance (UC5)

| Route | UC test | Ground test |
|---|---|---|
| `/governance/dashboard` | `_smoke.spec.ts` (post-login landing); `uc5-01-governance.spec.ts` step 3a (metric cards + trend chart) | `ground/shell/root-redirect.spec.ts` (`/` redirect target) |
| `/governance/metrics` | `uc5-01-governance.spec.ts` step 3b (list: metrics, type badges, Enabled) | — |
| `/governance/metrics/new` | `uc5-01-governance.spec.ts` step 1a (create form → redirect) | — |
| `/governance/metrics/[id]` | `uc5-01-governance.spec.ts` steps 1c, 2, 3c, 4 (Edit→PUT, Run, attr/conf+result+event, Delete) | — |

#### Validation (UC2)

| Route | UC test | Ground test |
|---|---|---|
| `/validation` | `uc2-01-validation.spec.ts` step 3 (cross-dataset list; URNs, score badges) | — |
| `/validation/data/[urn]` (redirects to `/data/[urn]`) | covered via `/data/[urn]` (see Per-dataset hub) | — |

#### Ontology Generation (UC3)

| Route | UC test | Ground test |
|---|---|---|
| `/ontogen` | `uc3-01-ontology-generation.spec.ts` step 4 (redirects to `/ontogen/result`) | — |
| `/ontogen/result` | `uc3-01-ontology-generation.spec.ts` steps 4, 4b (browser: Nodes/Edges/Triples/Graph tabs as compact tables, All/Approved/Unapproved status filter, Graph-tab force-directed canvas, revoke an approved row → reason-confirm dialog → rejected; result envelopes) | `ground/ontogen/result-table.spec.ts` (Created-At SortControl drives `?sort=created_at_asc\|_desc` on the node fetch; shared standard Pagination control present — Rows-per-page selector default 20 + Prev/Next; data-conditional: 6 compact columns + Evidence button → modal when rows exist) |
| `/ontogen/conf` | `uc3-01-ontology-generation.spec.ts` steps 1, 3 (Run + Edit conf, no Delete; Run dialog) | `ground/ontogen/conf-edit-no-submit.spec.ts` (clicking Edit enters edit mode and fires NO PUT /spoke/ontogen/attr/conf — real-browser guard for the morph-then-submit defect) |
| `/ontogen/seed` | `uc3-01-ontology-generation.spec.ts` steps 2, 5 (create seed, delete via ConfirmDialog) | — |

#### Metadata Generation (UC4)

MetaGen is a managed **collection** of confs (one global review queue). The use-case
arc creates two confs over different dataset groups, opts datasets in via per-dataset
boundaries, runs each conf, reviews candidates, and inspects the result queue +
uncovered view.

| Route | UC test | Ground test |
|---|---|---|
| `/metagen` (redirects to `/metagen/conf`) | `uc4-01-metadata-generation.spec.ts` step 1 (302 redirect → conf list) | — |
| `/metagen/conf` | `uc4-01-metadata-generation.spec.ts` steps 1, 2 (list heading, Create-conf link, both confs as rows) | `ground/metagen/conf-list.spec.ts` (list render, Create-conf link href, seeded-conf row + per-row Run) |
| `/metagen/conf/new` | `uc4-01-metadata-generation.spec.ts` step 2 (create conf EU + conf OE → redirect) | `ground/metagen/conf-new.spec.ts` (fill form → POST → redirect → backend read-back of fields + dataset_filter) |
| `/metagen/conf/[id]` | `uc4-01-metadata-generation.spec.ts` steps 4, 5 (Run via RunDialog, per-conf events isolation) | `ground/metagen/conf-edit-no-submit.spec.ts` (clicking Edit enters edit mode, fires NO PUT /spoke/metagen/conf/{id} — Edit/Save morph guard) |
| `/metagen/result` | `uc4-01-metadata-generation.spec.ts` step 6 (review-queue heading, item queue + cross-conf events, both runs in union) | `ground/metagen/result.spec.ts` (queue + events render; conf_id filter select narrows the GET /spoke/metagen/item request) |
| `/metagen/uncovered` | `uc4-01-metadata-generation.spec.ts` step 10 (heading + include_disallowed toggle; off=no_conf_match, on⊇off) | `ground/metagen/uncovered.spec.ts` (include_disallowed toggle flips query param off→on; off⊆on reason-classification invariant) |
| `/metagen/data/[urn]` (redirects to `/data/[urn]`) | covered via `/data/[urn]` (see Per-dataset hub) — steps 3, 7, 8, 8b, 9 (boundary form, item cards w/ conf_name badge, Approve/Reject candidate, cross-conf demotion, metagen events in unified Events panel) | — |

#### Admin / Account

| Route | UC test | Ground test |
|---|---|---|
| `/admin/conf` | `_smoke.spec.ts` (adminApi probe) | `ground/admin/conf.spec.ts` (form renders from GET conf; edit `validation_score_n_intervals` → Save → persist → revert); `ground/shell/admin-nav-*` (link visible to Admin, absent for editor/reader) |
| `/admin/users` | — | `ground/admin/users.spec.ts` (list; role change via Radix Select; delete via ⋯ ConfirmDialog); `ground/shell/admin-nav-hidden.reader.spec.ts` (Reader direct-nav → permission-denied) |
| `/profile` | — | `ground/account/profile.spec.ts` (email/role locked; change name → Save → confirm via /auth/me → revert) |
| `/profile/tokens` | — | `ground/account/tokens.spec.ts` (mint → dsk_ reveal → in list; revoke via ConfirmDialog → gone) |
| `/settings` | — | `ground/account/settings.spec.ts` (Theme Dark → `html.dark`; locale Select → localStorage) |
| `/` | `_smoke.spec.ts` (post-login landing) | `ground/shell/root-redirect.spec.ts` (`/` → `/governance/dashboard`) |

---

## App-shell role-gating

Real-session role behavior (the editor/reader/admin Playwright projects use real
provisioned sessions, not mocked `useMe`). Covered by `tests/e2e/ground/shell/`:

| Concern | Test file | Project |
|---|---|---|
| Admin nav section (Users + Configurations) visible; Account section visible; main feature nav visible; Admin above Account in DOM | `admin-nav-visible.spec.ts` | admin |
| Admin nav section absent; Account section visible; direct `/admin/users` nav → permission-denied | `admin-nav-hidden.reader.spec.ts` | reader |
| Admin nav section absent; Account section visible | `admin-nav-hidden.editor.spec.ts` | editor |
| Reader write control ("Create source") suppressed on `/ingestion/conf`; read-only content + sidebar "unmanaged" nav still render | `reader-write-suppressed.reader.spec.ts` | reader |
