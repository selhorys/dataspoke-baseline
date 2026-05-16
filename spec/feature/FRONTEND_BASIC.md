# DataSpoke Frontend — Shared Layer

> Conforms to [MANIFESTO](../MANIFESTO_en.md). API contract in [API.md](../API.md).
> Per-workspace specs: [FRONTEND_DE](FRONTEND_DE.md), [FRONTEND_DA](FRONTEND_DA.md),
> [FRONTEND_DG](FRONTEND_DG.md).

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
on `401`.

---

## Routing

| UI path | Purpose |
|---|---|
| `/` | Portal landing — links to DE, DA, DG cards |
| `/login` | Login page (consumes `/auth/token`) |
| `/de/...` | [Data Engineering workspace](FRONTEND_DE.md) |
| `/da/...` | [Data Analysis workspace](FRONTEND_DA.md) |
| `/dg/...` | [Data Governance workspace](FRONTEND_DG.md) |
| `/settings` | Theme + locale toggle, persisted in `localStorage` only (no API) |

The route guard reads the JWT `groups` claim and redirects to the portal when
the user is missing the workspace's required group.

```
┌─────────────────────────────────────────────────┐
│  DataSpoke              user@imazon  ▼  Logout  │
├─────────────────────────────────────────────────┤
│                                                 │
│   Pick a workspace:                             │
│                                                 │
│   ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│   │   DE     │   │   DA     │   │   DG     │    │
│   │Engineering   │  Analysis │   │Governance│    │
│   └──────────┘   └──────────┘   └──────────┘    │
│                                                 │
└─────────────────────────────────────────────────┘
                  Portal landing (`/`)
```

---

## Authentication

| User action | API call |
|---|---|
| Login | `POST /auth/token` with `{email, password}` |
| Token refresh on 401 | `POST /auth/token/refresh` (refresh token in HttpOnly cookie) |
| Logout | `POST /auth/token/revoke` |

Access token lives in memory (15 min lifetime). Refresh token is set as an
HttpOnly cookie by the API; the frontend never reads it. Logout clears the
in-memory access token and calls revoke.

```
┌─────────────────────────────────┐
│  DataSpoke — Sign in            │
├─────────────────────────────────┤
│  Email:    [                  ] │
│  Password: [                  ] │
│                                 │
│  [        Sign in        ]      │
└─────────────────────────────────┘
              Login (`/login`)
```

---

## Live Updates

The baseline API exposes no WebSocket or SSE channels. Live freshness is
polling-only via TanStack Query's `refetchInterval` against `event/...` and
`attr/.../result` endpoints (default 15 s on visible pages, paused on tab
blur). Frontend code MUST NOT introduce paths under `/spoke/.../stream/...`.

---

## Cross-Workspace Permission Gates

The API enforces only the JWT `groups` claim (see [API §Authentication](../API.md#authentication--authorization)).
Workspace boundaries hide actions that the API would technically accept but
that violate the project's review model:

| Action | Allowed in workspace | API call |
|---|---|---|
| Approve / reject ontogen node, edge, triple | DG only | `POST /spoke/common/ontogen/result/{node\|edge\|triple}/{id}/method/review` |
| Trigger ontogen / ingestion / metagen / metric runs | DE, DA, DG | `POST /spoke/.../method/run` |
| Edit `attr/conf` for any feature | DE, DA, DG | `PUT/PATCH/DELETE /spoke/.../attr/conf` |
| Approve / reject metagen candidates | DE, DA, DG | `POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` |

DE and DA render ontogen review **status** but hide the action buttons; the
governance team holds approval per UC3.

---

## Shared Component Notes

These component IDs are referenced from per-workspace specs.

- **OntologyNavigator** — flat node list with approved triples overlaid as
  labelled arrows. Reads `GET /spoke/common/ontogen/result/{node,edge,triple}`.
  Outgoing-triples-on-a-node is a client-side filter on the triple list (the
  API exposes only the standard pagination / sort / time-range query
  parameters from [API §Query Parameters](../API.md#query-parameters)).
  **Read-only**: the action button for `method/review` is rendered only when
  the host workspace permits approval (DG only).
- **NotificationCenter** — bell-icon popover that polls per-feature
  `event/...` endpoints (see [Live Updates](#live-updates)).
- **ConfirmDialog** — destructive-action gate (revoke token, delete config).
  No API of its own.
