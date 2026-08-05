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
on `401`. The API base URL is resolved at runtime, not inlined at build time,
so one image serves any environment.

Resolution runs per field, and both the browser and the server render must
reach the same answer — a server render that resolves differently ships an
href the client never corrects, because React leaves an already-rendered
attribute alone. Highest priority first:

| Source | Available to |
|---|---|
| `window.__DATASPOKE_RUNTIME_CONFIG__`, injected by the root layout per request | client only |
| `DATASPOKE_API_BASE_URL` / `DATASPOKE_AIRFLOW_URL` | server only — the injected global does not exist yet during SSR |
| `NEXT_PUBLIC_API_BASE_URL` / `NEXT_PUBLIC_AIRFLOW_URL` | both; a local-dev convenience, absent from the deployed image |
| `""` — same-origin API, no Airflow link | both |

An empty string counts as unset at every tier and falls through to the next,
so a deployment that sets a variable to `""` behaves as if it had not set it.
The `DATASPOKE_*` tier is deliberately non-`NEXT_PUBLIC_*`: Next.js inlines
only the latter, so keeping these server-side is what preserves the one-image
property above.

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
docs. Each icon renders only when its URL resolves non-empty. The DataHub icon
links to `<datahub_url>/login` (the `/login` suffix is DataHub-specific);
Langfuse and Airflow use the bare URL; ReDoc is `apiBaseUrl` + `/redoc`.

Each link resolves from exactly one source; which one depends on whether the
system is an externally-wired peripheral:

| Link | Source | Resolution |
|---|---|---|
| DataHub, Langfuse (URL + `langfuse_project_id`) | Peripheral | [`GET /spoke/common/peripheral-links`](../API.md#data-resource-spokecommondata) only |
| Airflow, ReDoc | Deployment-local | Runtime config only (`airflowUrl`, `apiBaseUrl`) |

`GET /spoke/common/peripheral-links` serves the `peripheral_config` DB plane, the
**sole** source of `datahub_url`, `langfuse_url`, and `langfuse_project_id` — the
client carries no alternative for these three values, so nothing can mask what the
DB holds. Peripheral wiring done in that plane
(`PATCH /admin/peripherals/{datahub,langfuse}`) therefore reaches the UI with no
chart operation and no pod restart. A resolved link is retained while the read
refreshes and across a failed refresh, so a wired icon never flashes away and back;
only a read that has never succeeded leaves the value unresolved. Airflow and ReDoc
instead come from runtime config because they are not externally wired: Airflow
ships in the umbrella chart and ReDoc is the API itself, so neither appears in
`peripheral_config`.

Operators control visibility by setting or omitting the URLs in `peripheral_config`,
so deployments that should not expose an infra UI simply leave them unset.

Both peripheral values are re-checked in the client against the display-link safety
rule ([`API.md` §Data Resource](../API.md#data-resource-spokecommondata)) before they reach an anchor `href`, and a
failing value resolves to `""` — the same "render no link" state as an unset one. The
client check backstops the API's coercion at the point of interpolation, so the value
is validated where it actually becomes an `href`.
`langfuse_project_id` follows the same resolution and deep-links evidence
references into their Langfuse trace sessions.

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
| `/oauth-error` | Landing page for a failed Google sign-in (`?error=<code>` query param) — see [OAuth error page](#oauth-error-page-oauth-error). Pure presentation of the query param | — (no API call) |
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

The table above indexes the shell, auth, admin, and per-feature **entry** routes. Feature
sub-routes — create and detail pages, and the per-feature `/{feature}/data/[urn]` deep-link
redirects into `/data/[urn]` — are enumerated in each feature spec's own Routes table, which is
authoritative for them: [Governance](FRONTEND_GOVERNANCE.md) (`/governance/metrics/new`,
`/governance/metrics/[id]`), [Ingestion](FRONTEND_INGESTION.md) (`/ingestion/sources/new`,
`/ingestion/sources/[id]`, `/ingestion/data/[urn]`), [MetaGen](FRONTEND_METAGEN.md)
(`/metagen/conf/new`, `/metagen/conf/[id]`, `/metagen/data/[urn]`), and
[Validation](FRONTEND_VALIDATION.md) (`/validation/data/[urn]`).

Route guards layer two checks:

- **JWT presence** — `/login`, `/register`, `/forgot-password`, `/reset-password`, `/oauth-error`, and the OAuth callback URL are public; all other routes redirect to `/login?next=<path>` when no access token is available. The login page honors `next` on success (default fallback `/governance/dashboard`).
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
HttpOnly cookie by the API; the frontend never reads it. Logout calls
`POST /auth/token/revoke`; only on success does it clear the in-memory access
token and navigate to `/login` — a failed revoke leaves the session live, so
the UI surfaces the error and keeps the user signed in rather than showing a
signed-out shell over a refresh cookie only the API can clear. The Google flow
is a full-page browser navigation — the SPA reloads itself at the callback URL
with tokens already attached, or at [`/oauth-error`](#oauth-error-page-oauth-error)
when either Google route fails.

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
│  Email:    alice@imazon (locked)│
│  Name:     [ Alice             ]│
│  Role:     Reader (read-only)   │
│  Google:   linked / not linked  │
│  Password: set / not set        │
│                                 │
│  ─── Change / Set a password ───│
│  New password: [              ] │
│                                 │
│  [    Save changes    ]         │
└─────────────────────────────────┘
             Profile (`/profile`)
```

`Role`, `Google`, and `Password` are read-only, driven by `role`, `has_google`,
and `has_password` from `GET /auth/me`. `role` is the DataSpoke `users.role`
column — the SSOT that gates every DataSpoke route; only an Admin can change it,
via `PATCH /admin/users/{id}/role`. The password section titles itself "Change password" when
`has_password` is true and "Set a password" when it is false — the latter is the
state a user lands in after signing in with Google bound onto a row that had one
([AUTH §Credential reset on link](AUTH.md#credential-reset-on-link)). Both write
through the same `PATCH /auth/me` `password` field.

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
"unlink Google" — `DELETE /admin/users/{id}/google` behind a `ConfirmDialog`
that states the consequence (the user's sessions end and they sign in again),
shown only for rows with `has_google` and disabled for rows without
`has_password`, since the route refuses those with
`409 GOOGLE_IS_ONLY_AUTH_METHOD` — and "manage tokens", a drawer listing the
user's `api_tokens` rows with per-token revoke buttons
(`GET /admin/users/{id}/api-tokens`,
`DELETE /admin/users/{id}/api-tokens/{token_id}`).

### OAuth error page (`/oauth-error`)

The Google routes are browser-navigation endpoints whose handlers 302 on every
outcome, so a failed sign-in arrives here rather than on an error envelope
([API §OAuth browser-redirect contract](../API.md#oauth-browser-redirect-contract)).
The page is public, makes no API call, and renders copy selected by the `error`
query parameter. Selection is a lookup into the fixed map below — the received
parameter value is never echoed into the rendered output, since the page is
directly navigable with any value:

| `error` | Copy |
|---|---|
| `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` | This address is already linked to a different Google account, plus the three-step recovery sequence — request and complete a password reset, ask an admin to unlink, then sign in with Google again ([AUTH §Admin unbind](AUTH.md#admin-unbind)). The only code whose copy is a procedure rather than a sentence, because it is a steady state the user cannot leave unaided. |
| `GOOGLE_ACCOUNT_LINKED_ELSEWHERE` | This Google account is already linked to another DataSpoke user; retry the sign-in, which resolves against the existing link, and ask an admin to release the link if it is stale ([AUTH §Admin unbind](AUTH.md#admin-unbind)). |
| `OAUTH_STATE_MISMATCH` | The sign-in attempt expired or was interrupted; start again from `/login`. |
| `OAUTH_EMAIL_NOT_VERIFIED` | Google has not verified this address; verify it with Google and retry. |
| `OAUTH_NOT_CONFIGURED` | Google sign-in is not configured on this deployment; use email + password and contact an administrator. |
| absent or unrecognised | Generic "Google sign-in could not be completed" wording. |

Every state carries a link back to `/login`, the only way onward from the page.

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
│  DataHub        ● Event stream OK 14:30  ● Metadata API OK   │
│    GMS URL        [ http://datahub-gms…              ]       │
│    Frontend URL   [ https://datahub.example.com      ]       │
│    Kafka brokers  [ broker:9092                      ]       │
│    Security protocol [ SASL_SSL ▾ ]                          │
│    SASL mechanism    [ SCRAM-SHA-512 ▾ ]                     │
│    SASL username  [ dataspoke                        ]       │
│    SASL password  [ •••••• leave blank to keep current ]    │
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
| DataHub | Frontend URL | `frontend_url` | Plain text; the **browser-facing DataHub UI URL, not GMS** — labelled to keep the two apart, since they differ in host, port, and scheme in most deployments. Feeds the shell's DataHub links |
| DataHub | Kafka brokers | `kafka_brokers` | Plain text; under `AWS_MSK_IAM` every host must be an MSK broker host (`kafka.<region>.amazonaws.com` or `kafka-serverless.<region>.amazonaws.com`), so the field carries that hint and the API rejects anything else |
| DataHub | Security protocol | `kafka_security_protocol` | Select — `PLAINTEXT` (default) / `SSL` / `SASL_PLAINTEXT` / `SASL_SSL` |
| DataHub | SASL mechanism | `kafka_sasl_mechanism` | Select — rendered only when the protocol is `SASL_*`. Under `SASL_SSL` the options are `PLAIN` / `SCRAM-SHA-256` / `SCRAM-SHA-512` / `AWS_MSK_IAM`; under `SASL_PLAINTEXT` the credential mechanisms only, since `AWS_MSK_IAM` requires `SASL_SSL` |
| DataHub | SASL username | `kafka_sasl_username` | Plain text; rendered only for `PLAIN` and `SCRAM-*` |
| DataHub | SASL password | `kafka_sasl_password` | Masked write-only secret (see below); rendered only for `PLAIN` and `SCRAM-*` |
| DataHub | AWS region | `kafka_aws_region` | Plain text, optional; rendered only for `AWS_MSK_IAM`. Blank means "derive from the broker hostname" |
| DataHub | Event stream health | `health` | Read-only badge in the card header (see below) |
| DataHub | Metadata API health | `api_health` | Read-only badge in the card header (see below) |
| DataHub | Token | `token` | Masked write-only secret (see below) |
| DataHub | Service corpuser URN | `service_corpuser_urn` | Non-secret, returned plain; default `urn:li:corpuser:dataspoke` |
| DataHub | Default env | `default_env` | Non-secret, returned plain; fabric/env, default `DEV` |
| Langfuse | Host | `host` | Plain text |
| Langfuse | Public key | `public_key` | Plain text |
| Langfuse | Secret key | `secret_key` | Masked write-only secret (see below) |
| Langfuse | Project ID | `project_id` | Non-secret, returned plain |
| Langfuse | Environment tag | `environment_tag` | Non-secret, returned plain |

- **The Kafka security fields are progressively disclosed.** The mechanism select
  appears only once the protocol is `SASL_*`, and the credential inputs only once
  the mechanism is a credential-based one. `PLAINTEXT` — the default — therefore
  shows nothing beyond the brokers, so an unsecured cluster is unchanged by this
  surface. The disclosure rules mirror the validation rules in
  [API.md §DataHub Kafka security](../API.md#datahub-kafka-security), which owns
  them — the form never offers a combination the API rejects with
  `422 INVALID_PARAMETER`, so a well-behaved form cannot produce one.
- **`AWS_MSK_IAM` shows no credential inputs at all**, and in their place an
  informational note: authentication uses the consumer pod's IAM role, which is
  attached at deploy time via the chart values `event-consumer.serviceAccount`
  (see [`spec/feature/HELM_CHART.md`](HELM_CHART.md#event-consumer-identity-and-rbac)).
  This restates the API rule that `kafka_sasl_username` and `kafka_sasl_password`
  are *rejected* under this mechanism, not merely unused. It is a real limit of
  the page, not an omission — the credential is a pod identity, so selecting the
  mechanism is the only part the UI can own.
- **Health badges** render the two read-only health objects from
  `GET /admin/peripherals/datahub` in the DataHub card header, each labelled for
  its plane so an operator can tell them apart: **Event stream** from `health`
  (the event consumer's report) and **Metadata API** from `api_health` (the sync
  sweep's report). Both use the same rendering — `ok` with `last_ok_at`, `error`
  with `last_error` as its detail, and `unknown` when nothing has reported yet.
  `unknown` is a neutral badge on **either** plane rather than a fault, because
  both reporters are opt-in: no event consumer is deployed by default, and the
  sync sweep's DAG ships paused. Saving does not refresh either badge; each moves
  when its own reporter next writes.
- **Masked secrets** (`token`, `kafka_sasl_password`, `secret_key`) use `PasswordInput` and behave like
  `llm_api_key` on `/admin/conf`: `GET` returns `""` (unset) or `"********"`
  (set); the field shows "leave blank to keep current"; an empty submission omits
  the field (unchanged), and the `"********"` sentinel is never echoed back as a
  written value. Secrets are routed to Kubernetes Secrets, not the DB.
- **Non-secret fields** (`service_corpuser_urn`, `default_env`, the visible
  `kafka_*` settings, `project_id`, `environment_tag`) are plain inputs or selects
  prefilled from the `GET` response and sent verbatim on `PATCH`.
  `kafka_sasl_password_version` is bookkeeping the API maintains — the page neither
  renders nor sends it.
- Each card's Save submits only the fields that changed within that card; the two
  cards never share a submit. A "Saved · updated <timestamp>" indicator appears in
  the saved card's footer (in-session only).
- The page is gated by the `useMe` admin check, like the other `/admin/*` pages.

**This page is the entry point of a fresh deployment.** Every read that resolves
through a feature service depends on the DataHub client, so until the DataHub
card is filled in those reads answer `503 PERIPHERAL_NOT_CONFIGURED` with
`detail.peripheral = "datahub"` ([API §Application Error Codes](../API.md#application-error-codes),
which scopes the code to DataHub-requiring endpoints). That is most of the
feature surface — notably including reads whose data lives only in DataSpoke's
own store, so an unwired DataHub is felt well beyond the pages that display
DataHub content. A few reads carry no such dependency and stay available:
`GET /spoke/common/peripheral-links`, which must, since it feeds the shell that
hosts the onboarding state and answers `""` for an unconfigured peripheral rather
than failing (see [API §Data Resource](../API.md#data-resource-spokecommondata));
`GET /spoke/ingestion/secrets`; and the MetaGen event feeds behind the
NotificationCenter.

This is the expected initial state of any deployment, not an error condition, and
the UI treats it as such: the affected pages render the muted
[QueryErrorState](#shared-component-notes) onboarding branch pointing back here,
and none of them burns retry backoff on it (see
[Query Error Policy](#query-error-policy)). Saving the DataHub card clears the
condition across the whole app with no pod restart, by the same
`peripheral_config` path the shell's links resolve through (see
[Shell](#shell)); pages already open pick it up on their next poll.

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

## Query Error Policy

One retry policy is set globally on the query client and governs every read;
per-hook overrides are the documented exception, not the norm. Failures split
into two classes:

- **Non-transient — no retry, fail immediately.** Any `4xx`, plus
  `PERIPHERAL_NOT_CONFIGURED` regardless of its `503` status
  ([API §Application Error Codes](../API.md#application-error-codes)). That code
  reports a configuration state rather than a fault: the peripheral stays
  unconfigured until an operator wires it, so a retry chain cannot change the
  answer and only spends seconds of backoff on every query in the app before
  arriving at the same result. `429` needs no separate rule — the `4xx` rule
  already covers it, and appropriately so, since its `Retry-After` is an
  instruction addressed to the caller and the query layer's blind backoff is the
  wrong instrument to honour it with.
- **Everything else — retried up to twice**, then surfaced to the render site.

The `4xx` rule sits above the fetch layer's own `401` refresh-and-replay (see
[Authentication](#authentication)): an expired access token is refreshed and the
request replayed beneath this policy, so only a `401` that survives that replay
reaches the query layer as a failure.

Two hooks tighten this further, each for a reason the global rule cannot express:

- The ingestion secret-resolver read (`GET /spoke/ingestion/secrets`) treats any
  `503` as final. That read exists to report whether the resolver is reachable at
  all, so an unavailable resolver is the answer rather than an obstacle to it
  (see [SECRET_RESOLUTION §Error taxonomy](SECRET_RESOLUTION.md#error-taxonomy)).
- The shell's peripheral-links read (`GET /spoke/common/peripheral-links`)
  retries once rather than twice: a failed refresh is already absorbed by the
  retain-last-resolved rule (see [Shell](#shell)), so further attempts change
  nothing a user can observe.

A page or panel that surfaces a failed read **inline** renders it through
[QueryErrorState](#shared-component-notes). Reads whose failure degrades to a
benign absence opt out and render nothing — peripheral-links and the links it
feeds resolve to `""`, the same "render no link" state as an unset value (see
[Shell](#shell)). A read that neither renders inline nor opts out falls back to
the global error toast.

Failing fast does not stop polling: a page on the standard 15 s
`refetchInterval` (see [Live Updates](#live-updates)) keeps re-issuing the read,
so it leaves an error or onboarding state on its own once the underlying
condition clears — no reload, no manual retry control.

Failed *mutations* keep the TanStack default of no retry and surface as toasts;
`PERIPHERAL_NOT_CONFIGURED` toasts with neutral rather than destructive styling,
since it names an unfinished setup step, but it is not suppressed — a write that
did not happen must still be reported.

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
  [DataHub dataset deep-link](#shared-component-notes) (`<datahub_url>/dataset/{urn}`,
  rendered only when the DataHub URL resolves non-empty).

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
- **DatasetFilterEditor** — parent-owned editor for the four-dimension
  `dataset_filter` (`origin` plus `tags[]` / `glossary_terms[]` /
  `dataset_urns[]`). Reused by Governance metrics, OntoGen conf, and MetaGen conf.
  Each list dimension is one **newline-separated** textarea — one URN per line —
  buffering the raw text the user typed; parsing happens on the way out (each
  line edge-trimmed, blank lines dropped, an empty dimension omitted from the
  filter) and parsed state is never re-serialised back into the box, so
  whitespace the user is mid-way through typing survives. Commas are **not**
  separators: tag and glossary-term URNs embed a user-authored name that may
  contain a comma, and dataset URNs always contain them. The editor reseeds its
  boxes from props only when the incoming filter is not the one it last emitted
  (e.g. a freshly loaded record).
- **DatasetFilterView** — read-only render of the four-dimension `dataset_filter`,
  the view-mode analogue of DatasetFilterEditor (empty dimensions show an em dash).
  List entries render monospaced with internal whitespace preserved, so a URN's
  own spacing reads back as stored. Reused by the Governance metric detail, and
  the OntoGen and MetaGen conf views.
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
  default Local). Every bound it emits is a canonical inclusive UTC ISO instant
  regardless. The trigger shows the preset's label (e.g. "Last 7 days")
  when a preset is selected, or the resolved bounds for a custom range —
  `YYYY-MM-DD – YYYY-MM-DD <tz>` (date) / `YYYY-MM-DD HH:mm – YYYY-MM-DD HH:mm <tz>`
  (datetime) — in the global timezone. A preset with no matching label falls
  back to that resolved-bounds form for the granularity with its open upper
  bound rendered as the literal `now` — `YYYY-MM-DD – now <tz>` (date) /
  `YYYY-MM-DD HH:mm – now <tz>` (datetime). Presets are **relative**: a preset
  stores intent rather than pinned bounds — a lower bound resolved against the
  present and an upper bound left open — so "Last 7 days" always includes today
  and everything recorded since; custom ranges are **absolute**. The selection **persists across visits** in
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
  API impact). Call sites resolve the selection to ISO-8601 bounds and map them
  to the query params each endpoint accepts (see
  [API §Query Parameters](../API.md#query-parameters)): endpoints whose end-bound
  param is `to` (events, governance metric `attr/result`) receive `from`/`to`
  directly, while the validation `attr/validation/result` endpoint — which names
  its end-bound `until` rather than `to` (see [API.md](../API.md),
  `attr/validation/result`) — takes the upper bound in that slot instead.
  **A preset resolves to an open-ended window** — the lower bound only, with
  `to`/`until` omitted — so the read always reaches the present, which is what
  lets a 15 s-polled panel (see [Live Updates](#live-updates)) surface records
  written after page load. **A custom range resolves to the closed inclusive
  pair** the user picked and keeps both bounds. Two consequences are accepted
  deliberately. First, a preset's *lower* bound is resolved against the clock at
  resolution time and then held — re-derived only when the selection or the
  display timezone changes, or on the next visit, never per render and never per
  poll tick, because it participates in the query key and re-resolving it per
  render would mint a new key every render and spin an unbounded refetch loop. A
  long-lived page's window therefore only ever **widens**, and past local
  midnight a `date` preset labelled "Last 7 days" may span eight calendar days. Second, validation results carry a caller-supplied
  `data_time`, so an open upper bound surfaces future-dated rows; a row dated
  ahead of the present is an anomaly worth surfacing, not hiding. Governance
  metric results are unaffected — their timestamp is server-stamped.
  The picker has no API of its own; it only shapes the query strings of the reads
  it drives.
- **ChartGrainPicker** — the **display-grain** control for every chart surface,
  placed in the heading row of the section whose charts it governs: the governance
  dashboard header (beside that page's [RangePicker](#shared-component-notes)),
  the governance metric detail `Result` panel header (beside that panel's
  RangePicker), and the per-dataset page's Validation panel `Quality Score`
  heading row (beside that row's RangePicker). It selects one of
  three grains — **hourly**, **daily** (default), **weekly** — governing how the
  rows a chart has already fetched are collapsed before plotting. Rows are
  bucketed into grain windows and each window contributes exactly **one** point:
  that window's **last** measurement (greatest timestamp), labelled by the
  truncated window start, carrying enough date component to stay unique across the
  selected range (hourly windows include the date, not the hour alone). Every x
  label is therefore distinct, and each point is drawn with a visible dot and an
  enlarged active dot, so a series of a single measurement renders as one visible
  point and every plotted measurement is hoverable. Window boundaries are derived
  in the **global Settings timezone preference** (Local or UTC, default Local) —
  the same one the RangePicker's calendar reads — so switching Local↔UTC
  re-derives the buckets; weekly windows start on **Monday** and are labelled by
  that Monday's date. A row whose timestamp does not parse contributes to no
  window and is dropped rather than grouped under a placeholder label; when two
  rows in a window carry the same timestamp the later one in the fetched order
  wins; and because the window label occupies the `date` key, a series named
  `date` is never plotted.
  Like the timezone preference itself, the grain is a **client-side display
  concern and adds no request parameter**: it never alters the `from` / `to` /
  `until` / `limit` a call site sends, and stored and queried timestamps remain
  canonical UTC ISO per `API.md`. The selection **persists across visits** in
  browser `localStorage` under a stable key per logical panel, by the same rule as
  the RangePicker selection — each panel keeps its own grain, shared across all
  entities of that panel type. The picker has no API of its own.
- **DatahubDatasetLink** — a shared external deep-link to a dataset's DataHub page,
  `<datahub_url>/dataset/{urn}` (URN URL-encoded). It resolves the DataHub URL from
  `GET /spoke/common/peripheral-links` by the same rule as the header icon
  (see [Shell](#shell)), and renders a labelled new-tab link (`_blank rel=noopener`)
  only when that URL is non-empty; otherwise nothing. Reused across the
  dataset tables (the Governance dataset catalog, the Ingestion unmanaged + source
  Datasets tables, the MetaGen uncovered table) and the per-dataset page header.
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
- **QueryErrorState** — the render point for a page or panel that surfaces a
  failed read inline, and the only one (see
  [Query Error Policy](#query-error-policy), which also covers the reads that
  opt out of inline rendering). It branches on the error:
  - When the error is `PERIPHERAL_NOT_CONFIGURED`, it names the peripheral from
    `detail.peripheral` and renders a **muted onboarding state** styled like the
    empty state, not the destructive error state — an unwired peripheral is a setup
    step the deployment has not reached, so presenting it in alarm styling misreads
    a normal first-run condition as a fault. Admins are directed to
    [`/admin/peripherals`](#peripherals-adminperipherals) and get a link there;
    non-admins are told to ask an administrator, with **no link**, because that
    route is Admin-gated and a link they cannot follow is worse than a sentence
    naming who can. The role-specific line is held until the role resolves, so an
    admin is never shown the non-admin wording.
  - For every other error it renders the ordinary destructive error state with the
    message from the API's error envelope.
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
