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
| `/login` | `_smoke.spec.ts` (auth harness) | `ground/auth/login.public.spec.ts` (form render, bad-creds error, Register link, Forgot-password link, and the **initial server HTML** — fetched over raw HTTP, not the post-hydration DOM — carrying an absolute `Sign in with Google` href whose host is the chart-configured API host and whose path is `/api/v1/auth/google/login`, independently probed for its contractual 302; cluster-frontend run mode only) |
| `/register` | — | `ground/auth/register.public.spec.ts` (form render, short-password validation, real signup → redirect, and the same **initial server HTML** absolute-href check on `Sign up with Google`) |
| `/forgot-password` | — | `ground/auth/password-reset.public.spec.ts` (form render, submit → confirmation state) |
| `/reset-password` | — | `ground/auth/password-reset.public.spec.ts` (no-token error state, dummy-token form render, short-password validation) |
| `/oauth-error` | — | `ground/auth/oauth-error.public.spec.ts` (public reachability, bound-elsewhere three-step recovery, sibling-code copy, absent-code fallback, hostile `?error=` not echoed, Back-to-sign-in link, and the real `/auth/google/callback` redirect target rendering this page) |

### App routes (authenticated)

#### Per-dataset hub (shared)

The unified per-dataset page merges the formerly separate `/ingestion/data`,
`/validation/data`, and `/metagen/data` surfaces — three summary cards
(Ingestion / Validation / MetaGen) + three foldable panels (Validation, MetaGen,
Events). The ingestion reverse-lookup folds into the Ingestion summary card (no
standalone Ingestion panel); the header carries a shared DataHub deep-link. The
retired per-feature `/{feature}/data/[urn]` routes are redirects to `/data/[urn]`.

| Route | UC test | Ground test |
|---|---|---|
| `/data/[urn]` | `uc1-02-active-custom-postgres.spec.ts` step 6 (Ingestion panel reverse-lookup); `uc2-01-validation.spec.ts` steps 2, 4–7 (Validation panel: conf, charts, hard-delete via ConfirmDialog with cascade — afterwards reads as a never-created Create empty-state, no Undelete/Show-deleted toggle; recreate via the Create form; validation events in unified Events panel); `uc4-01-metadata-generation.spec.ts` steps 3, 7, 8, 9 (MetaGen panel boundary form, candidate review, metagen events in unified Events panel) | `ground/data/hub.spec.ts` (URN header + 3 summary cards; 3 foldable panels fold/unfold + NO Ingestion panel; consolidated Ingestion card + header DataHub deep-link; Events major-type filter all-checked default, then — over a seeded INGESTION event and a seeded VALIDATION event on the same dataset — unchecking a major drops exactly its rows and keeps the others) |

#### Ingestion (UC1)

| Route | UC test | Ground test |
|---|---|---|
| `/ingestion` (redirects to `/ingestion/conf`) | — | `ground/ingestion/redirect.spec.ts` (bare `/ingestion` rests on `/ingestion/conf`; the source list renders there, dual-confirmed against `GET /spoke/ingestion/sources`) |
| `/ingestion/conf` | `uc1-02-active-custom-postgres.spec.ts` step 7; `uc1-03-passive-kafka.spec.ts` step 7; `uc1-01-datahub-managed.spec.ts` step 3 (list view, DataHub-managed filter; wrappers hidden) | `ground/shell/reader-write-suppressed.reader.spec.ts` (Reader read-only: "Create source" absent) |
| `/ingestion/sources/new` | `uc1-02-active-custom-postgres.spec.ts` step 1 (create ACTIVE_CUSTOM_MANAGED); `uc1-03-passive-kafka.spec.ts` step 1 (create PASSIVE) | — |
| `/ingestion/sources/[id]` | `uc1-02-active-custom-postgres.spec.ts` steps 2–7; `uc1-03-passive-kafka.spec.ts` steps 2–3, 5–7 (detail, run, datasets, passive_observation INGESTION.COMPLETE event row, events, delete); `uc1-01-datahub-managed.spec.ts` steps 4–6 (detail, run, datasets, events, read-only, INGESTION.COMPLETE event row, `high (pipeline_name)` authority cell after real execution) | `ground/ingestion/source-edit-no-submit.spec.ts` (read-mode renders the recipe as a highlighted `<pre>`, no textarea; clicking Edit swaps the recipe header to Save/Cancel + reveals the textarea and fires NO PUT — Edit/Save morph guard; header Save external-submit PUTs once + "Source updated" toast + REST read-back of the edited recipe) |
| `/ingestion/unmanaged` | `uc1-03-passive-kafka.spec.ts` steps 0, 4 (before/after source creation) | — |
| `/ingestion/data/[urn]` (redirects to `/data/[urn]`) | — | `ground/data/legacy-deep-link.spec.ts` (single-encoded deep link rests on `/data/<encoded urn>`, URN round-trips through the redirect, hub header intact) |

#### Governance (UC5)

| Route | UC test | Ground test |
|---|---|---|
| `/governance/dashboard` | `_smoke.spec.ts` (post-login landing); `uc5-01-governance.spec.ts` step 3a (metric cards + trend chart) | `ground/shell/root-redirect.spec.ts` (`/` redirect target); `ground/governance/dashboard-view-controls.spec.ts` (view controls: all types checked / blank search / ascending title order by default; deselecting one type drops its cards; case-insensitive substring title search, inactive while blank and blind to `description`; deselecting every type empties the grid into the view-controls empty state, not the enable-a-metric one; the three choices survive a reload; and none of them changes the metric-list read — every `GET /spoke/governance/metric` carries exactly `is_enabled=true` + `limit=100`) |
| `/governance/metrics` | `uc5-01-governance.spec.ts` step 3b (list: metrics, type badges, Enabled) | — |
| `/governance/metrics/new` | `uc5-01-governance.spec.ts` step 1a (create form → redirect) | — |
| `/governance/metrics/[id]` | `uc5-01-governance.spec.ts` steps 1c, 2, 3c, 4 (Edit→PUT, Run, Config+Result+Event, Delete) | `ground/governance/metric-grain.spec.ts` (Result panel ChartGrainPicker: Hourly/Daily/Weekly with Daily default; switching grain adds no request parameter — no `grain` in the `attr/result` query and the sent query strings unchanged; the choice survives a reload and carries to a second metric's Result panel — one stored grain per panel type) |
| `/governance/datasets` | — | `ground/governance/datasets.spec.ts` (Governance-menu navigation + 5 column headers incl. validation; dataset_urn → /data/[urn] link, datahub deep-link, click-through to the hub; dual-confirmed against GET /spoke/common/data) |

#### Validation (UC2)

| Route | UC test | Ground test |
|---|---|---|
| `/validation` | `uc2-01-validation.spec.ts` step 3 (cross-dataset list; URNs, score badges) | `ground/validation/coverage-filter.spec.ts` (covered/uncovered checkbox default state; none-checked → select-a-filter empty state; over a seeded conf'd dataset + a seeded no-conf dataset, covered lists the first and excludes the second and uncovered does the reverse, dual-confirmed against GET /spoke/validation?coverage=covered\|uncovered) |
| `/validation/data/[urn]` (redirects to `/data/[urn]`) | — | `ground/data/legacy-deep-link.spec.ts` (single-encoded deep link rests on `/data/<encoded urn>`, URN round-trips through the redirect, hub header intact) |

#### Ontology Generation (UC3)

| Route | UC test | Ground test |
|---|---|---|
| `/ontogen` | `uc3-01-ontology-generation.spec.ts` step 4 (redirects to `/ontogen/result`) | — |
| `/ontogen/result` | `uc3-01-ontology-generation.spec.ts` steps 4, 4b (browser: Nodes/Edges/Triples/Graph tabs as compact tables, All/Approved/Unapproved status filter, Evidence column → Langfuse session Link, Graph-tab force-directed canvas, revoke an approved row → reason-confirm dialog → rejected; result envelopes + per-row run_id) | `ground/ontogen/result-table.spec.ts` (Created-At SortControl drives `?sort=created_at_asc\|_desc` on the node fetch; shared standard Pagination control present — Rows-per-page selector default 20 + Prev/Next; data-conditional: 7 compact columns + Evidence column linking to `…/sessions/{run_id}` in a new tab when rows exist) |
| `/ontogen/conf` | `uc3-01-ontology-generation.spec.ts` steps 1, 3 (Run + Edit conf, no Delete; Run dialog) | `ground/ontogen/conf-edit-no-submit.spec.ts` (read-mode renders a plain-text VIEW — is_enabled value dual-confirmed vs REST, no form control in DOM; clicking Edit swaps to the form and fires NO PUT/PATCH `.../attr/conf` — real-browser guard for the morph-then-submit defect; header Save external-submit writes exactly once + "Configuration saved" toast + REST read-back, with the singleton conf snapshotted and restored) |
| `/ontogen/seed` | `uc3-01-ontology-generation.spec.ts` steps 2, 5 (create seed, delete via ConfirmDialog) | — |

#### Metadata Generation (UC4)

MetaGen is a managed **collection** of confs (one global review queue). The use-case
arc creates two confs over different dataset groups, opts datasets in via per-dataset
boundaries, runs each conf, reviews candidates, and inspects the result queue +
uncovered view.

| Route | UC test | Ground test |
|---|---|---|
| `/metagen` (redirects to `/metagen/conf`) | `uc4-01-metadata-generation.spec.ts` step 1 (302 redirect → conf list) | — |
| `/metagen/conf` | `uc4-01-metadata-generation.spec.ts` steps 1, 2 (list heading, Create-conf link, both confs as rows) | `ground/metagen/conf-list.spec.ts` (list render, Create-conf link href, seeded-conf row + per-row Run; schedule_tier "daily" cell links to Airflow DAG `metagen-daily`, the branch chosen by the injected runtime config's `airflowUrl`) |
| `/metagen/conf/new` | `uc4-01-metadata-generation.spec.ts` step 2 (create conf EU + conf OE → redirect) | `ground/metagen/conf-new.spec.ts` (fill form → POST → redirect → backend read-back of fields + dataset_filter) |
| `/metagen/conf/[id]` | `uc4-01-metadata-generation.spec.ts` steps 4, 5 (Run via RunDialog, per-conf events isolation) | `ground/metagen/conf-edit-no-submit.spec.ts` (read-mode renders a plain-text VIEW — overwrite_pending "yes", no form control in DOM; header Edit/Run/Delete; clicking Edit swaps to the form + hides Run/Delete + fires NO PUT — Edit/Save morph guard; header Save external-submit PUTs once + "Conf saved" toast + REST read-back) |
| `/metagen/result` | `uc4-01-metadata-generation.spec.ts` step 6 (result-rollup heading, per-dataset rollup + cross-conf events, both runs in union; GET /spoke/metagen/dataset row read-back) | `ground/metagen/result.spec.ts` (per-dataset rollup + events render; conf_id filter select narrows the GET /spoke/metagen/dataset request) |
| `/metagen/uncovered` | `uc4-01-metadata-generation.spec.ts` step 10 (heading + include_disallowed toggle; off=no_conf_match, on⊇off) | `ground/metagen/uncovered.spec.ts` (include_disallowed toggle flips the query param off→on; over a seeded enabled conf scoping one boundary-less dataset, that URN is absent from the off set and present as `boundary_blocked` in the on set, in both the API and the rendered table) |
| `/metagen/data/[urn]` (redirects to `/data/[urn]`) | the redirect **target** is exercised by `uc4-01-metadata-generation.spec.ts` steps 3, 7, 8, 8b, 9 (boundary form, per-kind ItemKindTable candidate rows w/ conf_name badge, column.description grouped by field_path, per-row Approve/Reject candidate, cross-conf demotion, metagen events in unified Events panel), which navigate to `/data/[urn]` directly | `ground/data/legacy-deep-link.spec.ts` (the redirect itself: single-encoded deep link rests on `/data/<encoded urn>`, URN round-trips through the redirect, hub header intact) |

#### Admin / Account

| Route | UC test | Ground test |
|---|---|---|
| `/admin/conf` | `_smoke.spec.ts` (adminApi probe) | `ground/admin/conf.spec.ts` (form renders from GET conf; edit `validation_score_n_intervals` → Save → persist → revert); `ground/shell/admin-nav-*` (link visible to Admin, absent for editor/reader) |
| `/admin/users` | — | `ground/admin/users.spec.ts` (list; role change via Radix Select; delete via ⋯ ConfirmDialog); `ground/shell/admin-nav-hidden.reader.spec.ts` (Reader direct-nav → permission-denied) |
| `/admin/peripherals` | — | `ground/admin/peripherals.spec.ts` (DataHub + Langfuse cards render from GET; non-secret fields prefilled, secret inputs blank; edit DataHub `default_env` → Save DataHub → persist confirmed via adminApi → revert; secret/`is_configured` untouched; **Kafka security sub-form** — progressive disclosure by `kafka_security_protocol`, `AWS_MSK_IAM` offered under `SASL_SSL` only, IAM swaps credentials for `kafka_aws_region` + the deploy-time-IAM note, all save-free; **two labelled health badges** — the Event stream badge's `data-status` mirrors `GET /admin/peripherals/datahub` `health.status` and the Metadata API badge's mirrors `api_health.status`, each with its own conditional `last_error` detail, so a conflated render of one row into both badges fails whenever the live rows disagree) |
| `/profile` | — | `ground/account/profile.spec.ts` (email/role locked; change name → Save → confirm via /auth/me → revert) |
| `/profile/tokens` | — | `ground/account/tokens.spec.ts` (**My-tokens scope** — mint → dsk_ reveal → in list; revoke via ConfirmDialog → gone. **Clipboard fallback** — with `navigator.clipboard` shadowed away (the plain-HTTP condition the dev deployment is already in), Copy token still reaches the copied state, raises no "Copy failed" toast, leaves no textarea behind, and leaves the one-shot reveal dialog open, still showing the `dsk_` token and still interactive (its own Done button closes it); jsdom has neither the Clipboard API nor `document.execCommand`, so this is the only layer that decides it. **All-tokens scope (Admin)** — the scope control renders and defaults to My tokens, All tokens adds Owner + Status and drops "New token", an Editor-owned token (minted in setup by signing in as that user, since minting is owner-only) is listed against its owner e-mail as `active` and revoked through `DELETE /admin/users/{id}/api-tokens/{token_id}`, leaves the default view, is confirmed revoked-not-deleted via `include_revoked=true`, and returns under "Show revoked" labelled `revoked` with no revoke action) |
| `/settings` | — | `ground/account/settings.spec.ts` (Theme Dark → `html.dark`; locale Select → localStorage) |
| `/` | `_smoke.spec.ts` (post-login landing) | `ground/shell/root-redirect.spec.ts` (`/` → `/governance/dashboard`) |

The app shell itself (rendered on every authenticated route) additionally carries
the peripheral infra-icon behavior covered in [App-shell peripheral links](#app-shell-peripheral-links);
the `/data/[urn]` DataHub deep-link case there complements the `ground/data/hub.spec.ts` row above.

---

## Known gaps (tracked, not covered)

| Gap | Spec | Why open |
|---|---|---|
| A record written **after** page load surfaces in a polled panel with **no navigation**. `spec/feature/FRONTEND_BASIC.md §shared-component-notes` says a preset's open-ended window "is what lets a 15 s-polled panel (see Live Updates) surface records written after page load" — the *outcome*, not just the emitted params. The three retry blocks that previously worked around the frozen upper bound (`ground/data/hub.spec.ts`, `uc2-01-validation.spec.ts`, `uc1-02-active-custom-postgres.spec.ts`) all seed **before** navigating, so their rows are already in the first fetch and the open window is only incidentally involved. `src/frontend/components/events-panel.test.tsx` and `src/frontend/lib/api/data.test.ts` prove the params stay open and the query key stays stable, but stop at the params boundary. | `spec/feature/FRONTEND_BASIC.md §shared-component-notes`, `§Live Updates` | Needs a live cluster; deferred out of the change that introduced the open window. Planned shape: a `ground/data/` test that navigates to `/data/[urn]`, expands the Events panel, POSTs a validation result through `adminApi`, then asserts the new row becomes visible with **no navigation** under a bounded `toBeVisible({ timeout: ≥ 30_000 })` (one 15 s poll tick plus slack). |
| A `dataset_filter` list entry whose URN carries internal spacing renders with that spacing **visually intact** (not collapsed) in `DatasetFilterView`, on the OntoGen and MetaGen conf views. `src/frontend/components/dataset-filter-view.test.tsx` asserts the exact text node plus the declared `whitespace-pre*` / `font-mono` classes, but jsdom loads no stylesheet, so the class is a proxy for the rendered result. | `spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterView` ("List entries render monospaced with internal whitespace preserved, so a URN's own spacing reads back as stored") | Needs a real browser to observe layout. Planned shape: a `ground/metagen/` test that PUTs a conf whose `dataset_filter.tags` entry contains a double space, opens the conf view, and asserts the entry's `getComputedStyle(...).whiteSpace` preserves spaces (or compares the entry's rendered width against a collapsed control). |

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

---

## App-shell peripheral links

The header infra icons and the per-dataset DataHub deep-link resolve their base
URL from the `peripheral_config` DB plane served by
`GET /spoke/common/peripheral-links`, which is its sole source — the client
carries no alternative, so a sentinel written through `PATCH /admin/peripherals/datahub`
is proof of provenance on its own. Covered by
`tests/e2e/ground/shell/peripheral-links.spec.ts` (admin project):

| Concern | Test |
|---|---|
| Header DataHub icon resolves from `peripheral_config.frontend_url` after a DB-plane-only PATCH, with no chart operation and no pod rollout | `header DataHub icon resolves from the peripheral_config frontend_url` |
| Per-dataset DataHub deep-link on `/data/[urn]` resolves from the same source, as a new-tab link | `dataset DataHub deep-link resolves from the peripheral_config frontend_url` |
