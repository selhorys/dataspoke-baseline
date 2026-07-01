# DataSpoke Frontend — Shared Layer

> Conforms to [MANIFESTO](../MANIFESTO_en.md). API contract in [API.md](../API.md).
> Per-function specs: [FRONTEND_GOVERNANCE](FRONTEND_GOVERNANCE.md),
> [FRONTEND_INGESTION](FRONTEND_INGESTION.md), [FRONTEND_VALIDATION](FRONTEND_VALIDATION.md),
> [FRONTEND_ONTOGEN](FRONTEND_ONTOGEN.md), [FRONTEND_METAGEN](FRONTEND_METAGEN.md).

DataSpoke is API-first. This frontend is a thin reference client over the routes
catalogued in `API.md`; UI elements MUST trace to a real API surface — invented
features (settings APIs, streaming endpoints, score axes, recommendation
panels) are out of scope.

---

## Stack

Next.js (App Router) + TypeScript + Tailwind. Server state via TanStack Query;
client state via Zustand; forms via React Hook Form. The API client at
`src/frontend/lib/api/client.ts` prepends `/api/v1`, attaches
`Authorization: Bearer <access_token>`, surfaces the standard error envelope
(`{error_code, message, trace_id}`) as typed errors, and triggers a refresh
on `401`. The API base URL is resolved at runtime (the server injects
`DATASPOKE_API_BASE_URL` into the page; empty falls back to same-origin),
not inlined at build time, so one image serves any environment.

---

## Design system

A small, deliberate visual vocabulary applied uniformly across every page.

### Color tokens

shadcn-style HSL CSS variables in `app/globals.css`, defined twice — for light
(`:root`) and dark (`.dark`). Components consume the tokens, never raw colors.
Exact values live in `globals.css`; this spec fixes only their roles and intent.

- **`--brand`** — the single indigo brand accent; `--primary` and `--ring`
  derive from it. Used with restraint: primary buttons, the active nav item,
  focus rings, and the open-panel chevron. Brand is a punctuation color, not a
  fill.
- **Semantic status tokens** — `--success` (green), `--warning` (amber), and
  `--info` (sky), each with a `*-foreground` pair. Status badges and
  status-variant helpers map to these rather than overloading `default` /
  `secondary`, so a status reads as a status. An unrecognized or empty status
  carries no semantic color: it falls back to the neutral `secondary` variant.
- **Feature hues** (the signature) — five muted per-feature accents, one each
  for Ingestion, Validation, OntoGen, MetaGen, and Governance. A feature hue
  appears only in three narrow places: a thin left "spine" on that feature's
  `CollapsiblePanel`, a tick on its summary card, and its sidebar group icon.
  This encodes the product truth that a dataset is viewed through five feature
  lenses (hub and spoke). The hues are intentionally desaturated so the five
  coexist without clashing and stay distinct from the success / warning / info /
  destructive status hues.

### Typography

A three-role font system wired via `next/font` in `app/layout.tsx` and exposed
as Tailwind families in `tailwind.config.ts`. Each role has one job:

- **Display** (`font-display`, Space Grotesk) — page titles and section / panel
  headers only.
- **Body** (`font-sans`, Inter) — default body text and dense tables.
- **Mono** (`font-mono`, JetBrains Mono) — URNs, run-ids, and code.

### Type scale

Three header registers establish hierarchy:

- **Page title** — `font-display text-2xl font-semibold tracking-tight`,
  unified across pages through a shared `PageHeader` component.
- **Section / panel header** — `font-display text-sm font-semibold` paired with
  a muted header bar, a divider when open, and the feature spine, so a section
  header is unmistakable even at small size.
- **Eyebrow labels** — field-group and sidebar section labels (e.g. Admin,
  Account) use `text-xs font-semibold uppercase tracking-wider`.

---

## Shell

A single application shell hosts every page. The shell has a top header
(product name, user menu, logout) and a left-side menu. The product name links
to `/governance/dashboard` (the post-login home). The menu lists the
MANIFESTO §2.1 feature pages at the top, then two labelled sections pinned to
the bottom: an **Admin** section and, below it, an **Account** section. The
Admin section (entries **Users**, **Configurations**, and **Peripherals**) renders only when the
caller's role is `Admin`; the Account section (Profile, API Tokens, Settings)
renders for everyone. The page area on the right renders the active route.

The header right cluster also carries infra shortcut icons (new-tab links) to
the surrounding systems: DataHub, Langfuse, Airflow, and the DataSpoke ReDoc API
docs. Each icon renders only when its URL is configured — DataHub/Langfuse/Airflow
from runtime config `datahubUrl`/`langfuseUrl`/`airflowUrl` (the
`DATASPOKE_{DATAHUB,LANGFUSE,AIRFLOW}_URL` env vars, `NEXT_PUBLIC_*` in host dev);
ReDoc from `apiBaseUrl` + `/redoc`. The DataHub icon links to `<datahubUrl>/login`
(the `/login` suffix is DataHub-specific); Langfuse and Airflow use the bare URL.
The same runtime config also carries `langfuseProjectId` (`DATASPOKE_LANGFUSE_PROJECT_ID`,
`NEXT_PUBLIC_*` in host dev), used to deep-link evidence references into their Langfuse
trace sessions.
Operators control visibility by setting or
omitting the URLs, so deployments that should not expose an infra UI simply leave
its URL unset.

```
┌────────────────────────────────────────────────────────┐
│ DataSpoke                 user@imazon ▼  Logout         │
├─────────────────┬──────────────────────────────────────┤
│ Governance ▾    │   (page content)                     │
│  Dashboard      │                                      │
│  Datasets       │                                      │
│  Metrics        │                                      │
│ Ingestion ▾     │                                      │
│  Config         │                                      │
│  Unmanaged      │                                      │
│ Validation      │                                      │
│ OntoGen ▾       │                                      │
│  Config         │                                      │
│  Seed           │                                      │
│  Result         │                                      │
│ MetaGen ▾       │                                      │
│  Config         │                                      │
│  Result         │                                      │
│  Uncovered      │                                      │
├─────────────────┤                                      │
│ ADMIN           │   (Admin role only)                  │
│  Users          │                                      │
│  Configurations │                                      │
│ ACCOUNT         │                                      │
│  Profile        │                                      │
│  API Tokens     │                                      │
│  Settings       │                                      │
└─────────────────┴──────────────────────────────────────┘
                  Application shell
```

**Content width.** Every page fills the full available content width — the
`<main>` area inside the shell that sits to the right of the menu and carries the
shell gutter. Pages do not impose their own narrow max-width. Multi-field forms
lay their fields out in a responsive grid (each field roughly *available width ÷
columns* wide): two columns from the small breakpoint up, collapsing to a single
column on narrow viewports. Fields that need the full row — textareas,
Markdown/YAML/recipe/code editors, full-width selects, and file inputs — span the
whole width. Single large-editor pages (the OntoGen seed Markdown editor, the
ingestion recipe YAML editor) remain one full-width field.

---

## Routing

| UI path | Purpose | API calls |
|---|---|---|
| `/` | 302 to `/governance/dashboard` (post-login home) | — |
| `/login` | Login page (email+password and Google sign-in) | `POST /auth/token`, `GET /auth/google/login` |
| `/register` | Self-service sign-up (email + name + password ≥ 10 chars) and Google sign-up | `POST /auth/register`, `GET /auth/google/login` |
| `/forgot-password` | Request a password-reset email | `POST /auth/password/reset/request` |
| `/reset-password` | Submit a new password using the token from the email link (`?token=…` query param). An "Invalid link" guard state renders before any API call when `token` is missing/empty; a "Password updated" state renders on success | `POST /auth/password/reset/confirm` |
| `/profile` | Own profile + change display name + change password | `GET /auth/me`, `PATCH /auth/me` |
| `/profile/tokens` | Long-lived API token management — list, mint (copy-once display), revoke | `GET /auth/api-tokens`, `POST /auth/api-tokens`, `DELETE /auth/api-tokens/{id}` |
| `/admin/users` | Admin user management — list, change name, change role, hard delete, revoke any token | `GET /admin/users`, `PATCH /admin/users/{id}`, `PATCH /admin/users/{id}/role`, `DELETE /admin/users/{id}`, `GET /admin/users/{id}/api-tokens`, `DELETE /admin/users/{id}/api-tokens/{token_id}` |
| `/admin/conf` | Admin runtime configuration — view and edit the singleton behavioral tunables, dependency-stub toggles, and LLM provider/model/key, plus a self-contained **Workflow schedules** section to pause/unpause the six DAG groups | `GET /admin/conf`, `PATCH /admin/conf`, `GET /admin/dags`, `PATCH /admin/dags/{group}` |
| `/admin/peripherals` | Admin peripheral connections — view and edit DataHub and Langfuse connection settings (two cards, per-card partial PATCH) | `GET /admin/peripherals/datahub`, `PATCH /admin/peripherals/datahub`, `GET /admin/peripherals/langfuse`, `PATCH /admin/peripherals/langfuse` |
| `/governance/dashboard` | [Governance dashboard — home](FRONTEND_GOVERNANCE.md) | `GET /spoke/governance/metric`, `GET /spoke/governance/metric/{id}/attr/result` |
| `/governance/datasets` | [Dataset catalog](FRONTEND_GOVERNANCE.md) — cross-feature dataset list (UI under Governance, API in common/data) | `GET /spoke/common/data` |
| `/governance/metrics` | [Metric configuration](FRONTEND_GOVERNANCE.md) | `/spoke/governance/metric/...` |
| `/ingestion` | 302 to `/ingestion/conf` | — |
| `/ingestion/conf` | [Ingestion Control — source list](FRONTEND_INGESTION.md) | `/spoke/ingestion/sources` |
| `/ingestion/unmanaged` | [Ingestion Control — unmanaged bucket](FRONTEND_INGESTION.md) | `/spoke/ingestion/unmanaged` |
| `/validation` | [Validation](FRONTEND_VALIDATION.md) | `/spoke/validation/...` |
| `/ontogen` | 302 to `/ontogen/result` | — |
| `/ontogen/result` | [Ontology Generation — browser + review](FRONTEND_ONTOGEN.md) | `/spoke/ontogen/result/...` |
| `/ontogen/conf` | [Ontology Generation — conf + run](FRONTEND_ONTOGEN.md) | `/spoke/ontogen/attr/conf`, `/spoke/ontogen/method/run` |
| `/ontogen/seed` | [Ontology Generation — seed library](FRONTEND_ONTOGEN.md) | `/spoke/ontogen/attr/seed/...` |
| `/metagen` | 302 to `/metagen/conf` | — |
| `/metagen/conf` | [Metadata Generation — conf list + run](FRONTEND_METAGEN.md) | `/spoke/metagen/conf/...` |
| `/metagen/result` | [Metadata Generation — per-dataset result rollup + events](FRONTEND_METAGEN.md) | `/spoke/metagen/{dataset,event}` |
| `/metagen/uncovered` | [Metadata Generation — uncovered datasets](FRONTEND_METAGEN.md) | `/spoke/metagen/uncovered` |
| `/data/[urn]` | [Unified per-dataset page](#per-dataset-page-dataurn) — summary cards (incl. the Ingestion reverse-lookup) + Validation/MetaGen/Events panels | `/spoke/common/data/{urn}/...` (attr, event, validation, metagen) |
| `/settings` | Theme, locale, and timezone (Local or UTC, **default Local**) toggles, persisted in `localStorage` only. The timezone preference is display-only — it governs how all dates and times are rendered across the app; stored and queried timestamps remain canonical UTC ISO per `API.md`. | — |

Route guards layer two checks:

- **JWT presence** — `/login`, `/register`, `/forgot-password`, `/reset-password`, and the OAuth callback URL are public; all other routes redirect to `/login?next=<path>` when no access token is available. The login page honors `next` on success (default fallback `/governance/dashboard`).
- **`users.role` (read from `GET /auth/me.role`)** — `/admin/*` is server-side gated by the API's role check (`role = 'Admin'`); the UI hides the admin-menu entry when the role is not `Admin`. Inside each function page, write actions (approve/reject buttons, edit forms, run triggers) are rendered only when `role ∈ {Editor, Admin}` — Reader users see read-only views. The API enforces the same gate via `403 READ_ONLY_ROLE` on write methods; the UI suppression is for UX hygiene, not security.

---

## Authentication

| User action | API call |
|---|---|
| Login (email + password) | `POST /auth/token` with `{email, password}` |
| Login (Google) | `GET /auth/google/login` → browser redirect to Google → callback → tokens issued |
| Register | `POST /auth/register` with `{email, name, password}` |
| Token refresh on 401 | `POST /auth/token/refresh` (refresh token in HttpOnly cookie) |
| Logout | `POST /auth/token/revoke` |
| Request password reset | `POST /auth/password/reset/request` with `{email}` |
| Confirm password reset | `POST /auth/password/reset/confirm` with `{token, new_password}` |
| Read own profile | `GET /auth/me` |
| Update name and/or password | `PATCH /auth/me` with `{name?, password?}` |

Access token lives in memory (15 min lifetime). Refresh token is set as an
HttpOnly cookie by the API; the frontend never reads it. Logout clears the
in-memory access token and calls revoke. The Google flow is a full-page
browser navigation — the SPA reloads itself at the callback URL with tokens
already attached.

Full lifecycle (link rules, partial-failure semantics, OAuth state cookie
contract) lives in [AUTH](AUTH.md).

```
┌─────────────────────────────────┐
│  DataSpoke — Sign in            │
├─────────────────────────────────┤
│  Email:    [                  ] │
│  Password: [                  ] │
│                                 │
│  [        Sign in        ]      │
│                                 │
│  ──────  or  ──────             │
│                                 │
│  [  Sign in with Google  ]      │
│                                 │
│  Need an account?  Register →   │
│  Forgot password?               │
└─────────────────────────────────┘
              Login (`/login`)
```

```
┌─────────────────────────────────┐
│  Profile                        │
├─────────────────────────────────┤
│  Email:  alice@imazon (locked)  │
│  Name:   [ Alice               ]│
│  Role:   Reader (DataHub)       │
│  Google: linked / not linked    │
│                                 │
│  ─── Change password ───        │
│  New password: [              ] │
│                                 │
│  [    Save changes    ]         │
└─────────────────────────────────┘
             Profile (`/profile`)
```

```
┌──────────────────────────────────────────────────────────────┐
│  Admin — Users                       [ Search...           ] │
├──────────────────────────────────────────────────────────────┤
│  Email              Name    Role     Created     Actions     │
│  alice@imazon       Alice   Admin ▾  2026-01-15  ✎  ⋯       │
│  bob@imazon         Bob     Editor ▾ 2026-01-20  ✎  ⋯       │
│  carol@imazon       Carol   Reader ▾ 2026-02-01  ✎  ⋯       │
└──────────────────────────────────────────────────────────────┘
         Admin user list (`/admin/users`)
```

Inline role dropdown writes `PATCH /admin/users/{id}/role`. A pencil icon
(aria-label "Edit name") opens a name-edit dialog writing
`PATCH /admin/users/{id}`. The `⋯` menu carries
hard delete (writes `DELETE /admin/users/{id}` behind a `ConfirmDialog`),
and "manage tokens" — a drawer listing the user's `api_tokens` rows with
per-token revoke buttons (`GET /admin/users/{id}/api-tokens`,
`DELETE /admin/users/{id}/api-tokens/{token_id}`).

### Configurations (`/admin/conf`)

A single form that reads the runtime configuration with `GET /admin/conf` and
saves edits with a partial `PATCH /admin/conf` (only changed fields). The field
set is exactly the conf contract in [API.md](../API.md) §`/admin/conf` — the
page does not invent fields. Fields are grouped for legibility:

```
┌──────────────────────────────────────────────────────────────┐
│  Admin — Configurations                                      │
├──────────────────────────────────────────────────────────────┤
│  LLM         provider [gemini      ]  model [gemini-3.5-…  ]  │
│              API key  [•••••• leave blank to keep current]   │
│  OntoGen     max iters [3]  debate turns [4]  rag k [5]       │
│              reviewer model [                              ]  │
│  MetaGen     max iters [3]  debate turns [4]  rag k [5]       │
│              reviewer model [        ]  confidence [0.70]     │
│              ontology rag  node [5]  edge [5]  triple [5]     │
│  Validation  score intervals [3]                             │
│  Stubs       ☐ redis  ☐ llm  ☐ pgvector  ☐ notifications     │
│  Auth        DataHub corp group [dataspoke-users          ]  │
│                       Saved · updated 14:32   [ Save changes ]│
└──────────────────────────────────────────────────────────────┘
       Runtime configuration (`/admin/conf`)
```

- **Numeric inputs** mirror the API bounds so out-of-range never reaches the
  server (the API still enforces them via `422`): `*_llm_max_iterations` 1–20,
  `*_debate_max_turns` 2–10, `*_rag_k` and `metagen_ontology_rag_*_k` 0–20,
  `metagen_confidence_threshold` 0.0–1.0, `validation_score_n_intervals` ≥ 1.
- **`stub_*` toggles** are booleans rendered as switches; they gate the four
  dependency stubs (redis, llm, pgvector, notifications).
- **`llm_api_key`** is a masked write-only secret: `GET` returns `""` (unset) or
  `"********"` (set); the input shows "leave blank to keep current"; submitting
  an empty string clears it, omitting it leaves it unchanged. The key is routed
  to the Kubernetes Secret, not the DB.
- The nullable `*_reviewer_model` fields clear on blank: an empty input is sent as
  `null` (reuse `llm_model`) when changed.
- After a successful save a "Saved · updated <timestamp>" indicator appears in
  the footer next to Save (in-session only).

#### Workflow schedules

A self-contained `Card` rendered as a sibling section **outside** the runtime-conf
`<form>` — it is operational schedule control (Airflow paused state), not a
behavioral tunable, so it does not share the form's single Save button. The card
reads `GET /admin/dags` and renders one checkbox per DAG group; each toggle fires
its own immediate `PATCH /admin/dags/{group}` and the section invalidates the
`["admin","dags"]` query on success. The six groups and labels come verbatim from
[API.md](../API.md) §`/admin/dags` — the page invents no rows.

```
┌──────────────────────────────────────────────────────────────┐
│  Workflow schedules                                          │
├──────────────────────────────────────────────────────────────┤
│  ☑ DataHub hourly sync        ☑ Metadata generation         │
│  ☑ Auth role sync             ☑ Metrics                     │
│  ☑ Active ingestion           ☐ Ontology generation         │
└──────────────────────────────────────────────────────────────┘
       Workflow schedules (`/admin/dags`)
```

| Checkbox label | `group` |
|---|---|
| DataHub hourly sync | `datahub_sync` |
| Auth role sync | `auth_role_sync` |
| Active ingestion | `ingestion_active` |
| Ontology generation | `ontogen` |
| Metadata generation | `metagen` |
| Metrics | `metrics` |

- The checkbox reads "Enabled" semantically: **checked = unpaused**. A toggle sends
  `PATCH /admin/dags/{group}` with `{paused: !checked}`.
- When a group's response carries `mixed: true` (members disagree), the checkbox
  renders **indeterminate** (Radix `Checkbox` indeterminate state); toggling out of
  indeterminate sends an explicit `paused` value to bring all members into line.
- The card reuses the shared `components/ui/checkbox.tsx` (no Switch component
  exists); it does not touch the app-shell nav or the Peripherals page.

### Peripherals (`/admin/peripherals`)

One page with two independent `Card`s — DataHub and Langfuse — each its own
`<form>` with its own Save button. Each card reads its peripheral with the
matching `GET /admin/peripherals/{datahub,langfuse}` and saves a partial
`PATCH` of only the changed fields. The field sets are exactly the peripheral
contracts in [API.md](../API.md) §`/admin/peripherals` — the page invents no
fields.

```
┌──────────────────────────────────────────────────────────────┐
│  Admin — Peripherals                                         │
├──────────────────────────────────────────────────────────────┤
│  DataHub                                                     │
│    GMS URL        [ http://datahub-gms…              ]       │
│    Kafka brokers  [ broker:9092                      ]       │
│    Token          [ •••••• leave blank to keep current ]    │
│    Service corpuser URN [ urn:li:corpuser:dataspoke  ]       │
│    Default env    [ DEV                              ]       │
│                       Saved · updated 14:32   [ Save ]       │
├──────────────────────────────────────────────────────────────┤
│  Langfuse                                                    │
│    Host           [ http://langfuse…                 ]       │
│    Public key     [ pk-lf-…                           ]       │
│    Secret key     [ •••••• leave blank to keep current ]    │
│    Project ID     [ default                          ]       │
│    Environment tag[ production                       ]       │
│                       Saved · updated 14:31   [ Save ]       │
└──────────────────────────────────────────────────────────────┘
       Peripheral connections (`/admin/peripherals`)
```

| Card | Field | API field | Notes |
|---|---|---|---|
| DataHub | GMS URL | `gms_url` | Plain text |
| DataHub | Kafka brokers | `kafka_brokers` | Plain text |
| DataHub | Token | `token` | Masked write-only secret (see below) |
| DataHub | Service corpuser URN | `service_corpuser_urn` | Non-secret, returned plain; default `urn:li:corpuser:dataspoke` |
| DataHub | Default env | `default_env` | Non-secret, returned plain; fabric/env, default `DEV` |
| Langfuse | Host | `host` | Plain text |
| Langfuse | Public key | `public_key` | Plain text |
| Langfuse | Secret key | `secret_key` | Masked write-only secret (see below) |
| Langfuse | Project ID | `project_id` | Non-secret, returned plain |
| Langfuse | Environment tag | `environment_tag` | Non-secret, returned plain |

- **Masked secrets** (`token`, `secret_key`) use `PasswordInput` and behave like
  `llm_api_key` on `/admin/conf`: `GET` returns `""` (unset) or `"********"`
  (set); the field shows "leave blank to keep current"; an empty submission omits
  the field (unchanged), and the `"********"` sentinel is never echoed back as a
  written value. Secrets are routed to Kubernetes Secrets, not the DB.
- **Non-secret fields** (`service_corpuser_urn`, `default_env`, `project_id`,
  `environment_tag`) are plain inputs prefilled from the `GET` response and sent
  verbatim on `PATCH`.
- Each card's Save submits only the fields that changed within that card; the two
  cards never share a submit. A "Saved · updated <timestamp>" indicator appears in
  the saved card's footer (in-session only).
- The page is gated by the `useMe` admin check, like the other `/admin/*` pages.

```
┌────────────────────────────────────────────────────────────────────────┐
│  Profile · API tokens                            [ + New token ]        │
├────────────────────────────────────────────────────────────────────────┤
│  Name          Role    Created     Last used   Expires      Actions     │
│  ci-jenkins    Editor  2026-04-01  2026-05-25  2026-07-01   Revoke      │
│  laptop-cli    Editor  2026-05-10  —           never        Revoke      │
└────────────────────────────────────────────────────────────────────────┘
       Own API tokens (`/profile/tokens`)

┌─────────────────────────────────────────────────┐
│  New API token                                  │
├─────────────────────────────────────────────────┤
│  Name:    [ ci-jenkins                       ]  │
│  Expiry:  [ never ▾ ] (or: 30 d / 90 d / 1 y) │
│                                                 │
│  [   Create   ]                                 │
├─────────────────────────────────────────────────┤
│  Your new token (copy it now — it won't be      │
│  shown again):                                  │
│                                                 │
│  dsk_AbCdEf1234ZyXw...   [ Copy ]               │
└─────────────────────────────────────────────────┘
            Token mint dialog (one-shot display)
```

The raw token is displayed exactly once, inside the create dialog. The
clipboard copy button is the primary action — the user must transfer the
token to wherever it will be used before closing the dialog. Closing
without copy means the user must revoke and re-mint.

---

## Live Updates

The baseline API exposes no WebSocket or SSE channels. Live freshness is
polling-only via TanStack Query's `refetchInterval` against `event/...` and
`attr/.../result` endpoints (default 15 s on visible pages, paused on tab
blur). Frontend code MUST NOT introduce paths under `/spoke/.../stream/...`.

---

## Per-dataset page (`/data/[urn]`)

The single hub for everything DataSpoke knows about one dataset. It supersedes the former
per-feature detail routes — `/ingestion/data/[urn]`, `/validation/data/[urn]`, and
`/metagen/data/[urn]` now **redirect** here (preserving deep links). It consumes only the
per-dataset `/spoke/common/data/{urn}/…` routes verbatim, no invented endpoints.

Layout, top to bottom:

```
┌──────────────────────────────────────────────────────────────┐
│  catalog.books                               (dataset header)│
├──────────────────────────────────────────────────────────────┤
│  ┌── Ingestion ──┐  ┌── Validation ──┐  ┌── MetaGen ──┐       │
│  │ source / mode │  │ latest score / │  │ boundary    │       │
│  │ last-run ●ok  │  │ status         │  │ on? · N cand│       │
│  └───────────────┘  └────────────────┘  └─────────────┘       │
├──────────────────────────────────────────────────────────────┤
│  ▸ Validation  (conf editor + score/variables charts)         │
│  ▸ MetaGen     (boundary form + item/candidate review)        │
│  ▾ Events      [INGESTION][VALIDATION][METAGEN] [RangePicker]  │
│      one table — all event types, newest first  [Pagination]  │
└──────────────────────────────────────────────────────────────┘
```

- **Header row** — the dataset URN title, followed by the shared
  [DataHub dataset deep-link](#shared-component-notes) (`<datahubUrl>/dataset/{urn}`,
  rendered only when `datahubUrl` is configured).

- **Summary cards** — three horizontal cards giving an at-a-glance status:
  - *Ingestion* — owning source / ingestor, mode, and the latest-run **time** and status,
    from `GET …/attr/ingestion`. The source name links to its `/ingestion/sources/[id]`
    detail. This card carries the reverse-lookup content in full — there is no separate
    Ingestion foldable panel.
  - *Validation* — latest score / status, from `GET …/attr/validation/conf` +
    most-recent `…/attr/validation/result`. Alongside the score it shows that result's
    `data_time` (`results[0].data_time`) formatted with the shared tz/datetime helper.
  - *MetaGen* — boundary `is_enabled` and candidate count, from
    `GET …/attr/metagen/boundary` + `…/attr/metagen/item`. The count reads the
    item-list response's dataset-level `candidate_count` aggregate (total candidates
    of any status), so it matches the number the result rollup reports for the
    same dataset.

- **Three foldable panels** — each a [CollapsiblePanel](#shared-component-notes):
  - *Validation* — the `ValidationDataPanel` conf read-only / edit / create editor plus
    the score and per-variable charts
    (see [FRONTEND_VALIDATION](FRONTEND_VALIDATION.md)).
  - *MetaGen* — the `MetagenDataPanel`, with a **Boundary Config** sub-section
    (`GET/PUT/PATCH …/attr/metagen/boundary`; its `is_enabled` and `allowed` fields render as two
    outlined group boxes laid out horizontally in a single row) and a **Generated Items**
    sub-section (`GET …/attr/metagen/item`, item/candidate review) (see
    [FRONTEND_METAGEN](FRONTEND_METAGEN.md#per-dataset-dataurn-metagen-panel)).
  - *Events* — the unified [EventsPanel](#shared-component-notes): one table over
    `GET …/event` (the complete per-dataset timeline — ingestion runs ∪ validation ∪ metagen,
    newest first), driven by an [EventMajorTypeFilter](#shared-component-notes) (default all
    checked), a `datetime` [RangePicker](#shared-component-notes), and
    [Pagination](#shared-component-notes). Ingestion rows that originate on a linked wrapper carry
    a "wrapper" tag; the `detail` cell truncates the compact JSON and is click-to-expand into a
    pretty-printed dialog.

Write actions inside each panel (validation edits, boundary edits, candidate review) follow the
same role gating as their source feature — rendered only for `role ∈ {Editor, Admin}`; the API
enforces the same via `403 READ_ONLY_ROLE`. Panels poll on the standard 15 s interval, paused on
tab blur (see [Live Updates](#live-updates)).

---

## Shared Component Notes

These component IDs are referenced from per-function specs.

- **OntologyGraph** — interactive force-directed graph of the ontology. Reads
  `GET /spoke/ontogen/result/node` (graph nodes) and `GET .../result/triple`
  (links, source/target = subject/object node). Nodes are colored by status and
  sized by degree; supports drag, zoom/pan, and hover-highlight of neighbors. A
  client-side filter selects All or Approved-only. **Read-only** — review
  actions live in the OntoGen Nodes/Edges/Triples tables, not on the graph.
- **NotificationCenter** — bell-icon popover that merges the global
  cross-feature event feeds (`GET /spoke/ontogen/event`,
  `GET /spoke/metagen/event`) on one poll (see [Live Updates](#live-updates)).
  Governance exposes only per-metric feeds, so it is not aggregated here.
- **DatasetFilterEditor** — controlled editor for the four-dimension
  `dataset_filter` (`origin` plus `tags[]` / `glossary_terms[]` /
  `dataset_urns[]`). Reused by Governance metrics, OntoGen conf, and MetaGen conf.
- **DatasetFilterView** — read-only render of the four-dimension `dataset_filter`,
  the view-mode analogue of DatasetFilterEditor (empty dimensions show an em dash).
  Reused by the OntoGen and MetaGen conf views.
- **ScheduleTierLink** — renders a schedule tier (hourly / daily / weekly) as a
  link to its backing Airflow DAG, or plain text for an unscheduled / custom value.
  Reused by Ingestion (`ingestion-active-<tier>`), MetaGen (`metagen-<tier>`), and
  OntoGen (`ontogen-<tier>`). Links only when the runtime-config Airflow host and a
  DAG id are both present; target `_blank rel=noopener`.
- **RangePicker** — the single time-window control for every time-windowed
  surface (validation detail results + events, governance metric detail results
  + events, governance dashboard, ingestion source events, the per-dataset page's
  unified Events panel). It holds a **selection**, not a fixed range: either a named preset —
  Last 1 day, Last 7 days, **Last 2 weeks (default)**, Last 4 weeks, Last 12
  weeks — or a custom calendar range. Two granularities: `date` (calendar only)
  for daily timeseries surfaces, and `datetime` (calendar + start/end time) for
  event-log surfaces; in `datetime` the start/end time fields are 24-hour
  (`HH:mm`, no AM/PM). The picker has **no per-panel timezone control**: like
  all timestamps in the UI, the calendar days and times it shows are interpreted
  and displayed in the **global Settings timezone preference** (Local or UTC,
  default Local). The emitted/queried bounds remain canonical inclusive UTC ISO
  regardless. The trigger shows the preset's label (e.g. "Last 7 days")
  when a preset is selected, or the resolved bounds for a custom range —
  `YYYY-MM-DD – YYYY-MM-DD` (date) / `YYYY-MM-DD HH:mm – YYYY-MM-DD HH:mm`
  (datetime) — in the global timezone. Presets are **relative**: each visit re-resolves a preset to a
  window ending at the current day, so "Last 7 days" always includes today;
  custom ranges are **absolute**. The selection **persists across visits** in
  browser `localStorage` under a stable key per logical panel — each panel
  (e.g. validation results vs. validation events) persists independently and the
  preference is shared across all entities of that panel type — so revisiting a
  panel restores the last-used selection. The picker's popover presents the preset
  shortcuts alongside two calendars — a start-day calendar on the left and an
  end-day calendar on the right, each with independent month and year
  navigation. Every edit in the popover, including clicking a preset, is
  **staged** and takes effect only on **Apply**; **Cancel** discards staged
  edits. Clicking a preset stages it rather than applying immediately or closing
  the popover, and a staged preset still commits as a relative preset on Apply
  (keeping its label and relative-renewal behavior) only if untouched; any
  explicit calendar-day or time edit turns the staged preset into a **custom**
  absolute range (so an edited time is kept on Apply). The custom-range calendar renders the chosen span as a
  highlighted band with emphasized start and end days (a UI affordance with no
  API impact). Call sites resolve the selection to an inclusive `{from, to}`
  ISO-8601 pair and map it to the query params each endpoint accepts (see
  [API §Query Parameters](../API.md#query-parameters)): endpoints whose end-bound
  param is `from`/`to` (events, governance metric `attr/result`) receive `from`
  and `to` directly, while the validation `attr/validation/result` endpoint —
  which names its end-bound `until` rather than `to` (see
  [API.md](../API.md), `attr/validation/result`) — receives `until = to`. It has
  no API of its own; it only shapes the query strings of the reads it drives.
- **DatahubDatasetLink** — a shared external deep-link to a dataset's DataHub page,
  `<datahubUrl>/dataset/{urn}` (URN URL-encoded), from runtime config `datahubUrl`.
  It renders a labelled new-tab link (`_blank rel=noopener`) only when `datahubUrl` is
  set, mirroring the header infra-link gating; otherwise nothing. Reused across the
  dataset tables (the Governance dataset catalog, the Ingestion unmanaged + source
  Datasets tables, the MetaGen uncovered table) and the per-dataset page header. It has
  no API of its own.
- **CollapsiblePanel** — a titled, foldable section used to compose the
  [per-dataset page](#per-dataset-page-dataurn)'s foldable panels. Header row with a
  fold/unfold chevron over a body that mounts its feature panel; follows the existing
  `rounded-lg border` section styling.
- **EventMajorTypeFilter** — a checkbox-group multi-select over the event major types
  (`INGESTION` / `VALIDATION` / `METAGEN`), default **all checked**. Maps each checked box to a
  repeated `event_major_type` query param on `GET …/event`; with all checked it sends none
  (server returns all). Used by the per-dataset Events panel.
- **EventsPanel** — the unified per-dataset event table over `GET /spoke/common/data/{urn}/event`
  (the complete timeline — ingestion ∪ validation ∪ metagen). Composes an
  [EventMajorTypeFilter](#shared-component-notes), a `datetime`
  [RangePicker](#shared-component-notes) (→ `from`/`to`), and
  [Pagination](#shared-component-notes) (→ `offset`/`limit`/`total_count`), and renders each row's
  `event_type`, `occurred_at`, `status`, and a click-to-expand `detail` cell; ingestion rows whose
  derived `wrapper` flag is set carry a "wrapper" tag.
- **ConfirmDialog** — destructive-action gate (revoke token, delete config).
- **Pagination** — the single pagination control for every paged table across all
  features. It exposes a **page-size selector** (20 / 50 / 100, default **20**), **Prev /
  Next** buttons, **numbered pages** (with ellipsis for long ranges), and an **"M–N of T"**
  label. It is driven by and maps directly onto the standard `offset` / `limit` /
  `total_count` pagination envelope (see
  [API §Query Parameters](../API.md#query-parameters) and
  [API_DESIGN_PRINCIPLE §5](../API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination)):
  Prev/Next and numbered pages move `offset` in `limit`-sized steps, the size selector
  sets `limit` and resets `offset` to `0`, and the label and page count derive from
  `total_count`. The default size of `20` matches the API's own `limit` default. It holds
  no API of its own — it only shapes the `offset`/`limit` query params of the list read it
  drives.

---

## Testability

The automated E2E layer (`tests/e2e/`, see [TESTING](../TESTING.md#end-to-end-e2e-testing))
drives this UI with Playwright using user-facing locators (role, label, text). Components expose a
`data-testid` only where a semantic locator is insufficient — recharts widgets, dynamic table
rows, and status badges that carry no accessible name. Default to accessible markup (labelled
inputs, `role`-bearing controls, button text) so most flows need no test-id at all; add test-ids
narrowly when requested by the E2E author, not pre-emptively across the tree.
