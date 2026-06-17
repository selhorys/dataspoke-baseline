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

#### Ingestion (UC1)

| Route | UC test | Ground test |
|---|---|---|
| `/ingestion` | `uc1-02-active-custom-postgres.spec.ts` step 7; `uc1-03-passive-kafka.spec.ts` step 6; `uc1-01-datahub-managed.spec.ts` step 3 (list view, DATAHUB_MANAGED filter) | `ground/shell/reader-write-suppressed.reader.spec.ts` (Reader read-only: "Create source" absent) |
| `/ingestion/sources/new` | `uc1-02-active-custom-postgres.spec.ts` step 1 (create ACTIVE_CUSTOM_MANAGED); `uc1-03-passive-kafka.spec.ts` step 1 (create PASSIVE) | — |
| `/ingestion/sources/[id]` | `uc1-02-active-custom-postgres.spec.ts` steps 2–7; `uc1-03-passive-kafka.spec.ts` steps 2–3, 5–6; `uc1-01-datahub-managed.spec.ts` steps 4–6 (detail, run, datasets, events, read-only, INGESTION.COMPLETE event row, pipeline_name/high authority cell after real execution) | — |
| `/ingestion/unmanaged` | `uc1-03-passive-kafka.spec.ts` steps 0, 4 (before/after source creation) | — |
| `/ingestion/data/[urn]` | `uc1-02-active-custom-postgres.spec.ts` step 6 (reverse-lookup) | — |

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
| `/validation/data/[urn]` | `uc2-01-validation.spec.ts` steps 2, 4–7 (conf, charts, event log, delete, create, resurrect) | — |

#### Ontology Generation (UC3)

| Route | UC test | Ground test |
|---|---|---|
| `/ontogen` | `uc3-01-ontology-generation.spec.ts` step 4 (redirects to `/ontogen/result`) | — |
| `/ontogen/result` | `uc3-01-ontology-generation.spec.ts` step 4 (browser: Nodes/Edges/Triples/Navigator tabs, result envelopes) | — |
| `/ontogen/conf` | `uc3-01-ontology-generation.spec.ts` steps 1, 3 (Run + Edit conf, no Delete; Run dialog) | — |
| `/ontogen/seed` | `uc3-01-ontology-generation.spec.ts` steps 2, 5 (create seed, delete via ConfirmDialog) | — |

#### Metadata Generation (UC4)

| Route | UC test | Ground test |
|---|---|---|
| `/metagen` | `uc4-01-metadata-generation.spec.ts` steps 1, 3, 4, 9 (conf, Run+RunDialog, event panel, second run) | — |
| `/metagen/data/[urn]` | `uc4-01-metadata-generation.spec.ts` steps 2, 5, 6, 8 (boundary, item cards, Approve/Reject, events) | — |

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
| Reader write control ("Create source") suppressed on `/ingestion`; read-only content still renders | `reader-write-suppressed.reader.spec.ts` | reader |
