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

## Shell

A single application shell hosts every page. The shell has a top header
(product name, user menu, logout) and a left-side menu. The menu lists the
MANIFESTO §2.1 feature pages at the top, then two labelled sections pinned to
the bottom: an **Admin** section and, below it, an **Account** section. The
Admin section (entries **Users** and **Configurations**) renders only when the
caller's role is `Admin`; the Account section (Profile, API Tokens, Settings)
renders for everyone. The page area on the right renders the active route.

The header right cluster also carries infra shortcut icons (new-tab links) to
the surrounding systems: DataHub, Langfuse, Airflow, and the DataSpoke ReDoc API
docs. Each icon renders only when its URL is configured — DataHub/Langfuse/Airflow
from runtime config `datahubUrl`/`langfuseUrl`/`airflowUrl` (the
`DATASPOKE_{DATAHUB,LANGFUSE,AIRFLOW}_URL` env vars, `NEXT_PUBLIC_*` in host dev);
ReDoc from `apiBaseUrl` + `/redoc`. Operators control visibility by setting or
omitting the URLs, so deployments that should not expose an infra UI simply leave
its URL unset.

```
┌─────────────────────────────────────────────────────┐
│ DataSpoke              user@imazon ▼  Logout        │
├──────────────┬──────────────────────────────────────┤
│ Governance ▾ │   (page content)                     │
│  Dashboard   │                                      │
│  Metrics     │                                      │
│ Ingestion    │                                      │
│ Validation   │                                      │
│ OntoGen      │                                      │
│ MetaGen      │                                      │
├──────────────┤                                      │
│ ADMIN        │   (Admin role only)                  │
│  Users       │                                      │
│  Configs.    │                                      │
│ ACCOUNT      │                                      │
│  Profile     │                                      │
│  API Tokens  │                                      │
│  Settings    │                                      │
└──────────────┴──────────────────────────────────────┘
                  Application shell
```

---

## Routing

| UI path | Purpose | API calls |
|---|---|---|
| `/` | 302 to `/governance/dashboard` (post-login home) | — |
| `/login` | Login page (email+password and Google sign-in) | `POST /auth/token`, `GET /auth/google/login` |
| `/register` | Self-service sign-up (email + name + password ≥ 10 chars) and Google sign-up | `POST /auth/register`, `GET /auth/google/login` |
| `/forgot-password` | Request a password-reset email | `POST /auth/password/reset/request` |
| `/reset-password` | Submit a new password using the token from the email link (`?token=…` query param) | `POST /auth/password/reset/confirm` |
| `/profile` | Own profile + change display name + change password | `GET /auth/me`, `PATCH /auth/me` |
| `/profile/tokens` | Long-lived API token management — list, mint (copy-once display), revoke | `GET /auth/api-tokens`, `POST /auth/api-tokens`, `DELETE /auth/api-tokens/{id}` |
| `/admin/users` | Admin user management — list, change name, change role, hard delete, revoke any token | `GET /admin/users`, `PATCH /admin/users/{id}`, `PATCH /admin/users/{id}/role`, `DELETE /admin/users/{id}`, `GET /admin/users/{id}/api-tokens`, `DELETE /admin/users/{id}/api-tokens/{token_id}` |
| `/admin/conf` | Admin runtime configuration — view and edit the singleton behavioral tunables, dependency-stub toggles, and LLM provider/model/key | `GET /admin/conf`, `PATCH /admin/conf` |
| `/governance/dashboard` | [Governance dashboard — home](FRONTEND_GOVERNANCE.md) | `GET /spoke/governance/metric`, `GET /spoke/governance/metric/{id}/attr/result` |
| `/governance/metrics` | [Metric configuration](FRONTEND_GOVERNANCE.md) | `/spoke/governance/metric/...` |
| `/ingestion` | [Ingestion Control](FRONTEND_INGESTION.md) | `/spoke/ingestion/...` |
| `/validation` | [Validation](FRONTEND_VALIDATION.md) | `/spoke/validation/...` |
| `/ontogen` | [Ontology Generation](FRONTEND_ONTOGEN.md) | `/spoke/ontogen/...` |
| `/metagen` | [Metadata Generation](FRONTEND_METAGEN.md) | `/spoke/metagen/...` |
| `/settings` | Theme + locale toggle, persisted in `localStorage` only | — |

Route guards layer two checks:

- **JWT presence** — `/login`, `/register`, `/forgot-password`, `/reset-password`, and the OAuth callback URL are public; all other routes redirect to `/login` when no access token is available.
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
│  Admin · Users                       [ Search...           ] │
├──────────────────────────────────────────────────────────────┤
│  Email              Name    Role     Created     Actions     │
│  alice@imazon       Alice   Admin ▾  2026-01-15  edit  ⋯     │
│  bob@imazon         Bob     Editor ▾ 2026-01-20  edit  ⋯     │
│  carol@imazon       Carol   Reader ▾ 2026-02-01  edit  ⋯     │
└──────────────────────────────────────────────────────────────┘
         Admin user list (`/admin/users`)
```

Inline role dropdown writes `PATCH /admin/users/{id}/role`. "edit" opens an
inline name editor writing `PATCH /admin/users/{id}`. The `⋯` menu carries
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
│  Admin · Configurations                  updated 2026-05-29  │
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
│                                            [ Save changes ]  │
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
- The response `updated_at` is shown after a successful save.

```
┌──────────────────────────────────────────────────────────────┐
│  Profile · API tokens                  [ + New token ]       │
├──────────────────────────────────────────────────────────────┤
│  Name              Role    Created     Last used     Actions │
│  ci-jenkins        Editor  2026-04-01  2026-05-25    Revoke  │
│  laptop-cli        Editor  2026-05-10  —             Revoke  │
└──────────────────────────────────────────────────────────────┘
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

## Shared Component Notes

These component IDs are referenced from per-function specs.

- **OntologyNavigator** — flat node list with approved triples overlaid as
  labelled arrows. Reads `GET /spoke/ontogen/result/{node,edge,triple}`.
  Outgoing-triples-on-a-node is a client-side filter on the triple list (the
  API exposes only the standard pagination / sort / time-range query
  parameters from [API §Query Parameters](../API.md#query-parameters)).
  **Read-only**: the action button for `method/review` is rendered only when
  the caller's role permits approval (Editor / Admin).
- **NotificationCenter** — bell-icon popover that merges the global
  cross-feature event feeds (`GET /spoke/ontogen/event`,
  `GET /spoke/metagen/event`) on one poll (see [Live Updates](#live-updates)).
  Governance exposes only per-metric feeds, so it is not aggregated here.
- **DatasetFilterEditor** — controlled editor for the four-dimension
  `dataset_filter` (`origin` plus `tags[]` / `glossary_terms[]` /
  `dataset_urns[]`). Reused by Governance metrics, OntoGen conf, and MetaGen conf.
- **ConfirmDialog** — destructive-action gate (revoke token, delete config).
  No API of its own.

---

## Testability

The automated E2E layer (`tests/e2e/`, see [TESTING](../TESTING.md#end-to-end-e2e-testing))
drives this UI with Playwright using user-facing locators (role, label, text). Components expose a
`data-testid` only where a semantic locator is insufficient — recharts widgets, dynamic table
rows, and status badges that carry no accessible name. Default to accessible markup (labelled
inputs, `role`-bearing controls, button text) so most flows need no test-id at all; add test-ids
narrowly when requested by the E2E author, not pre-emptively across the tree.
