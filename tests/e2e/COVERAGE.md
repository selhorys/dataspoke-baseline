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
| `/governance/dashboard` | `_smoke.spec.ts` (post-login landing) | — |
| `/governance/metrics` | — | — |
| `/governance/metrics/new` | — | — |
| `/governance/metrics/[id]` | — | — |

#### Validation (UC2)

| Route | UC test | Ground test |
|---|---|---|
| `/validation` | — | — |
| `/validation/data/[urn]` | — | — |

#### Ontology Generation (UC3)

| Route | UC test | Ground test |
|---|---|---|
| `/ontogen` | — | — |
| `/ontogen/conf` | — | — |
| `/ontogen/seed` | — | — |

#### Metadata Generation (UC4)

| Route | UC test | Ground test |
|---|---|---|
| `/metagen` | — | — |
| `/metagen/data/[urn]` | — | — |

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
`/register`, `/forgot-password`, `/reset-password`, `/governance/metrics*`,
`/validation*`, `/ontogen*`, `/metagen*`, `/admin/users`, `/settings`,
`/profile*`
