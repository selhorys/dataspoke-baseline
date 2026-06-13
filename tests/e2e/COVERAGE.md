# E2E Route Coverage Map

Maps every route under `src/frontend/app/` to its covering E2E test(s).
Two groups cover the full surface: **use-case** (mirrors api-wired UC tests) and
**ground** (mirrors spot integration tests — narrow per-page flows).

`spec: spec/TESTING.md §End-to-End (E2E) Testing — use-case + ground groups together
cover every route; tracked here as the acceptance artifact for "fully covered".`

---

## Legend

- **UC test**: `tests/e2e/use-case/<file>.spec.ts` covering this route
- **Ground test**: `tests/e2e/ground/<feature>/<file>.spec.ts` covering this route
- `(partial)` — some but not all gestures/scenarios for this route are covered
- `—` — not yet covered (deferred to ground group or future UC)

---

## Routes

### Public routes (`/login`, `/register`, etc.)

| Route | UC test | Ground test |
|---|---|---|
| `/login` | `_smoke.spec.ts` (auth harness) | — |
| `/register` | — | — |
| `/forgot-password` | — | — |
| `/reset-password` | — | — |

### App routes (authenticated)

#### Ingestion (UC1)

| Route | UC test | Ground test |
|---|---|---|
| `/ingestion` | `uc1-active-custom-postgres.spec.ts` step 7 (list after delete); `uc1-passive-kafka.spec.ts` step 6 (list after delete); `uc1-datahub-managed.spec.ts` step 3 (list view, DATAHUB_MANAGED filter) | — |
| `/ingestion/sources/new` | `uc1-active-custom-postgres.spec.ts` step 1 (create ACTIVE_CUSTOM_MANAGED); `uc1-passive-kafka.spec.ts` step 1 (create PASSIVE) | — |
| `/ingestion/sources/[id]` | `uc1-active-custom-postgres.spec.ts` steps 2–7 (detail, dry-run, real-run, datasets, events, delete); `uc1-passive-kafka.spec.ts` steps 2–3, 5–6 (detail, run panel, datasets, events); `uc1-datahub-managed.spec.ts` steps 4–5 (detail, read-only, datasets) | — |
| `/ingestion/unmanaged` | `uc1-passive-kafka.spec.ts` steps 0, 4 (before/after source creation) | — |
| `/ingestion/data/[urn]` | `uc1-active-custom-postgres.spec.ts` step 6 (reverse-lookup; source + mode + latest_run) | — |

#### Governance (UC5)

| Route | UC test | Ground test |
|---|---|---|
| `/governance/dashboard` | `_smoke.spec.ts` (post-login landing); `uc5-governance.spec.ts` step 3a (metric cards + trend chart after run) | — |
| `/governance/metrics` | `uc5-governance.spec.ts` step 3b (list: all three metrics, type badges, Enabled status) | — |
| `/governance/metrics/new` | `uc5-governance.spec.ts` step 1a (create form: metric_id, type, title, description, metrics checkboxes, schedule, is_enabled, Save → redirect) | — |
| `/governance/metrics/[id]` | `uc5-governance.spec.ts` steps 1c (Edit → PUT → read-only), 2 (Run → ConfirmDialog), 3c (attr/conf dl, attr/result chart, event log, Edit/Run/Delete buttons), 4 (Delete → ConfirmDialog → list redirect) | — |

#### Validation (UC2)

| Route | UC test | Ground test |
|---|---|---|
| `/validation` | `uc2-validation.spec.ts` step 3 (cross-dataset list; both URNs, score badges) | — |
| `/validation/data/[urn]` | `uc2-validation.spec.ts` steps 2, 4–7 (detail: conf, charts, event log, delete, create, resurrect) | — |

#### Ontology Generation (UC3)

| Route | UC test | Ground test |
|---|---|---|
| `/ontogen` | `uc3-ontology-generation.spec.ts` steps 3–4 (Run dialog, tabs, result panels) | — |
| `/ontogen/conf` | `uc3-ontology-generation.spec.ts` step 1 (Edit/Save conf, is_enabled+schedule_tier) | — |
| `/ontogen/seed` | `uc3-ontology-generation.spec.ts` steps 2, 5 (create seed, delete via ConfirmDialog) | — |

#### Metadata Generation (UC4)

| Route | UC test | Ground test |
|---|---|---|
| `/metagen` | `uc4-metadata-generation.spec.ts` steps 1, 3, 4, 9 (conf form, Run button + RunDialog, global event panel, second run) | — |
| `/metagen/data/[urn]` | `uc4-metadata-generation.spec.ts` steps 2, 5, 6, 8 (boundary form, item cards, candidate Approve/Reject + ConfirmDialog, per-dataset events) | — |

#### Admin / Settings / Profile

| Route | UC test | Ground test |
|---|---|---|
| `/admin/conf` | `_smoke.spec.ts` (adminApi probe on GET /admin/conf) | — |
| `/admin/users` | — | — |
| `/settings` | — | — |
| `/profile` | — | — |
| `/profile/tokens` | — | — |

---

## Coverage delta — UC1 addition (this session)

Routes newly covered by `tests/e2e/use-case/uc1-*.spec.ts`:

| Route | Newly covered by |
|---|---|
| `/ingestion` | `uc1-active-custom-postgres.spec.ts`, `uc1-passive-kafka.spec.ts`, `uc1-datahub-managed.spec.ts` |
| `/ingestion/sources/new` | `uc1-active-custom-postgres.spec.ts`, `uc1-passive-kafka.spec.ts` |
| `/ingestion/sources/[id]` | all three UC1 files |
| `/ingestion/unmanaged` | `uc1-passive-kafka.spec.ts` |
| `/ingestion/data/[urn]` | `uc1-active-custom-postgres.spec.ts` |

Routes remaining uncovered by E2E (ground group deferred):
`/register`, `/forgot-password`, `/reset-password`, `/admin/users`, `/settings`, `/profile*`

---

## Coverage delta — UC5 addition (this session)

Routes newly covered by `tests/e2e/use-case/uc5-governance.spec.ts`:

| Route | Newly covered by |
|---|---|
| `/governance/dashboard` | `uc5-governance.spec.ts` step 3a |
| `/governance/metrics` | `uc5-governance.spec.ts` step 3b |
| `/governance/metrics/new` | `uc5-governance.spec.ts` step 1a |
| `/governance/metrics/[id]` | `uc5-governance.spec.ts` steps 1c, 2, 3c, 4 |

---

## Coverage delta — UC2 addition (this session)

Routes newly covered by `tests/e2e/use-case/uc2-validation.spec.ts`:

| Route | Newly covered by |
|---|---|
| `/validation` | `uc2-validation.spec.ts` step 3 |
| `/validation/data/[urn]` | `uc2-validation.spec.ts` steps 2, 4–7 |

---

## Coverage delta — UC3 addition (this session)

Routes newly covered by `tests/e2e/use-case/uc3-ontology-generation.spec.ts`:

| Route | Newly covered by |
|---|---|
| `/ontogen` | `uc3-ontology-generation.spec.ts` steps 3–4 (Run dialog, Nodes/Edges/Triples tabs, error-free panel render) |
| `/ontogen/conf` | `uc3-ontology-generation.spec.ts` step 1 (heading, Edit flow, is_enabled checkbox, schedule_tier Radix Select, Save configuration toast) |
| `/ontogen/seed` | `uc3-ontology-generation.spec.ts` steps 2, 5 (+New Seed → SeedEditor → Save seed toast; seed row Delete → ConfirmDialog → Seed deleted toast) |

---

## Coverage delta — UC4 addition (this session)

Routes newly covered by `tests/e2e/use-case/uc4-metadata-generation.spec.ts`:

| Route | Newly covered by |
|---|---|
| `/metagen` | steps 1, 3, 4, 9: conf form (is_enabled/schedule_tier/result_limit/overwrite_pending), Run button → RunDialog → run toast, global event panel (METAGEN.RUN_COMPLETE), second run |
| `/metagen/data/[urn]` | steps 2, 5, 6, 8: boundary form (is_enabled/allowed checkboxes), item cards (dataset.description / column.description grouping), candidate Approve → ConfirmDialog → approved toast, candidate Reject → ConfirmDialog → rejected toast, per-dataset event section (CANDIDATE_APPROVE / CANDIDATE_REJECT) |
