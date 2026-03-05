# DataSpoke Frontend — Basic Layout & Common Features

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Architecture context in [ARCHITECTURE](../ARCHITECTURE.md).
> API routes in [API](API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Technology Decisions](#technology-decisions)
3. [Application Shell](#application-shell)
4. [Routing](#routing)
5. [Authentication](#authentication)
6. [Settings](#settings)
7. [Help & Onboarding](#help--onboarding)
8. [Shared Components](#shared-components)
9. [State Management](#state-management)
10. [Real-Time Connectivity](#real-time-connectivity)

---

## Overview

The DataSpoke UI is a **portal-style** Next.js application. Users enter through a landing page that routes them to their user-group workspace (DE, DA, DG). Common features — authentication, settings, help, notifications — are shared across all workspaces.

```
src/frontend/
├── app/
│   ├── (auth)/          # Login, token refresh
│   ├── (portal)/        # Landing, group selection
│   ├── de/              # Data Engineering workspace
│   ├── da/              # Data Analysis workspace
│   ├── dg/              # Data Governance workspace
│   └── settings/        # User preferences
├── components/
│   ├── layout/          # Shell, sidebar, header, footer
│   ├── common/          # DataTable, SearchBar, StatusBadge, etc.
│   └── charts/          # Chart wrappers (Recharts/Highcharts)
├── lib/
│   ├── api/             # API client (fetch wrapper, auth interceptor)
│   ├── hooks/           # Shared React hooks
│   ├── store/           # Global state (Zustand)
│   └── ws/              # WebSocket connection manager
└── styles/              # Tailwind config, theme tokens
```

---

## Technology Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Framework | Next.js 15 (App Router) | SSR, file-based routing, React Server Components |
| Language | TypeScript (strict) | Type safety across API boundaries |
| Styling | Tailwind CSS | Utility-first, design-token driven theming |
| Charts | Recharts (default), Highcharts (complex) | Recharts for standard charts; Highcharts for graph/3D views |
| State | Zustand | Lightweight, no boilerplate, works with SSR |
| Data fetching | TanStack Query | Cache, retry, pagination, optimistic updates |
| Forms | React Hook Form + Zod | Validation schema reuse with API types |
| Icons | Lucide React | Consistent, tree-shakeable |

---

## Application Shell

All authenticated pages share a shell layout: top header + collapsible sidebar + content area.

```
┌────────────────────────────────────────────────────────────┐
│  [=]  DataSpoke          [?] [bell] [sun/moon] [avatar v] │
├─────────┬──────────────────────────────────────────────────┤
│         │                                                  │
│  Logo   │                                                  │
│         │                                                  │
│  ─────  │              Content Area                        │
│  Nav    │              (page-specific)                     │
│  Items  │                                                  │
│         │                                                  │
│  ─────  │                                                  │
│  Group  │                                                  │
│  Switch │                                                  │
│         │                                                  │
├─────────┴──────────────────────────────────────────────────┤
│  DataSpoke v0.1.0 │ Connected to DataHub ● │ Status: OK   │
└────────────────────────────────────────────────────────────┘
```

### Header

| Element | Behavior |
|---------|----------|
| `[=]` hamburger | Toggle sidebar collapse |
| Title | "DataSpoke" — links to portal landing |
| `[?]` help | Open contextual help panel |
| `[bell]` notifications | Notification popover (unread count badge) |
| `[sun/moon]` theme | Toggle light/dark mode |
| `[avatar v]` user menu | Dropdown: profile, settings, logout |

### Sidebar

Navigation items change per user group. The sidebar shows:
- **Logo** — DataSpoke icon
- **Group badge** — Current workspace (DE / DA / DG), color-coded
- **Nav items** — Group-specific pages (see FRONTEND_DE/DA/DG specs)
- **Group switch** — Quick-switch to another workspace (if user has multiple group claims)

Collapsed state shows icons only. Sidebar state persists via `localStorage`.

### Footer

Status bar showing: app version, DataHub connection status (via `GET /health`), system readiness.

---

## Routing

| Path | Purpose | Auth |
|------|---------|------|
| `/login` | Login page | Public |
| `/` | Portal landing — group selection | Required |
| `/de/*` | Data Engineering workspace | `groups` includes `de` |
| `/da/*` | Data Analysis workspace | `groups` includes `da` |
| `/dg/*` | Data Governance workspace | `groups` includes `dg` |
| `/settings` | User preferences | Required |

### Portal Landing

Displayed after login. Shows available workspaces based on user's `groups` claim.

```
┌────────────────────────────────────────────────────────┐
│                                                        │
│               Welcome, Maria Garcia                    │
│          Choose your workspace                         │
│                                                        │
│    ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│    │          │  │          │  │          │           │
│    │   (DE)   │  │   (DA)   │  │   (DG)   │          │
│    │   Data   │  │   Data   │  │   Data   │          │
│    │  Engin.  │  │ Analysis │  │  Govern. │          │
│    │          │  │          │  │          │           │
│    └──────────┘  └──────────┘  └──────────┘          │
│                                                        │
│    Cards greyed out if user lacks group claim          │
└────────────────────────────────────────────────────────┘
```

Users with a single group are auto-redirected to that workspace.

---

## Authentication

Uses JWT tokens issued by `POST /auth/token`. See [API.md §Authentication](API.md#authentication--authorization) for the full auth model.

### Login Page

```
┌──────────────────────────────────────┐
│                                      │
│           DataSpoke                  │
│                                      │
│    ┌──────────────────────────┐     │
│    │  Email                   │     │
│    └──────────────────────────┘     │
│    ┌──────────────────────────┐     │
│    │  Password                │     │
│    └──────────────────────────┘     │
│                                      │
│    [ Sign In ]                       │
│                                      │
│    Forgot password?                  │
└──────────────────────────────────────┘
```

### Token Lifecycle

| Event | Action |
|-------|--------|
| Login | `POST /auth/token` → store access token in memory, refresh token as HttpOnly cookie |
| API call | Attach `Authorization: Bearer <access_token>` header via API client interceptor |
| Token expiry (15 min) | Interceptor catches 401 → `POST /auth/token/refresh` → retry original request |
| Refresh failure | Redirect to `/login` |
| Logout | `POST /auth/token/revoke` → clear memory, redirect to `/login` |

### Route Guards

Next.js middleware checks JWT validity before rendering protected routes. If `groups` claim lacks the required group for the route prefix (`/de`, `/da`, `/dg`), redirect to portal landing with a toast message.

---

## Settings

Accessible from user menu → "Settings" or `/settings`. Persisted in `localStorage` (client-only prefs) and optionally in PostgreSQL via a future user preferences API.

```
┌─────────────────────────────────────────────────┐
│  Settings                                       │
├─────────────────────────────────────────────────┤
│                                                 │
│  Appearance                                     │
│  ┌─────────────────────────────────────┐       │
│  │ Theme:    ( ) Light  (●) Dark       │       │
│  │ Language: [English        v]        │       │
│  └─────────────────────────────────────┘       │
│                                                 │
│  Notifications                                  │
│  ┌─────────────────────────────────────┐       │
│  │ [x] Validation alerts               │       │
│  │ [x] Metric threshold alarms         │       │
│  │ [ ] Ingestion run completions       │       │
│  └─────────────────────────────────────┘       │
│                                                 │
│  Default Workspace                              │
│  ┌─────────────────────────────────────┐       │
│  │ [Data Engineering (DE) v]           │       │
│  └─────────────────────────────────────┘       │
│                                                 │
│  [ Save ]                                       │
└─────────────────────────────────────────────────┘
```

### Setting Categories

| Category | Options | Storage |
|----------|---------|---------|
| Theme | Light / Dark / System | `localStorage` |
| Language | English / Korean | `localStorage` |
| Notification prefs | Toggle per event type | `localStorage` (future: API) |
| Default workspace | DE / DA / DG | `localStorage` |
| Sidebar collapsed | Boolean | `localStorage` |
| Table page size | 10 / 20 / 50 / 100 | `localStorage` |

Dark mode is implemented via Tailwind's `dark:` variant. Theme toggle switches `class="dark"` on `<html>`.

---

## Help & Onboarding

### Contextual Help Panel

The `[?]` button opens a slide-over panel on the right. Content is context-aware — it shows help relevant to the current page.

```
┌─────────────────────────────────────────┬──────────┐
│                                         │  Help    │
│              Content Area               │  ──────  │
│                                         │          │
│                                         │  This    │
│                                         │  page    │
│                                         │  shows   │
│                                         │  ...     │
│                                         │          │
│                                         │  Links:  │
│                                         │  - Docs  │
│                                         │  - API   │
│                                         │          │
│                                         │  [Close] │
└─────────────────────────────────────────┴──────────┘
```

Help content is stored as static markdown keyed by route path. No external help service required.

---

## Shared Components

Reusable across all workspaces. Located in `src/frontend/components/common/`.

### DataTable

Paginated, sortable, filterable table component. Used for listing datasets, configs, events, metrics.

```
┌───────────────────────────────────────────────────────┐
│  [Search...               ]  [Filter v]  [Export v]   │
├──────┬────────────────────┬──────────┬────────────────┤
│  ☐   │  Name         ▽   │  Status  │  Updated    ▽  │
├──────┼────────────────────┼──────────┼────────────────┤
│  ☐   │  title_master      │  ● OK    │  2 hours ago   │
│  ☐   │  user_ratings      │  ▲ Warn  │  1 day ago     │
│  ☐   │  eu_profiles       │  ● OK    │  30 min ago    │
├──────┴────────────────────┴──────────┴────────────────┤
│  Showing 1-20 of 143       [< 1 2 3 ... 8 >]         │
└───────────────────────────────────────────────────────┘
```

- Pagination via `offset`/`limit` query params
- Sort via `sort` query param (e.g., `quality_score_desc`)
- Bulk selection via checkboxes for batch operations

### StatusBadge

Color-coded status indicator used throughout.

| Status | Color | Icon |
|--------|-------|------|
| OK / Healthy | Green | ● |
| Warning / Degraded | Amber | ▲ |
| Error / Critical | Red | ✕ |
| Running / In Progress | Blue | ◌ (spinner) |
| Unknown | Grey | ? |

### SearchBar

Natural language search input with type-ahead suggestions. Submits to `GET /spoke/common/search?q=...`.

### NotificationCenter

Popover from the bell icon. Lists recent events (validation alerts, metric alarms, ingestion completions). Events arrive via WebSocket or are polled.

### ConfirmDialog

Modal dialog for destructive actions (delete config, revoke token, etc.).

---

## State Management

| Scope | Tool | Examples |
|-------|------|---------|
| Server state | TanStack Query | API responses, paginated lists, dataset details |
| Client state | Zustand | Sidebar open/closed, active workspace, modal visibility |
| Form state | React Hook Form | Config editors, login form |
| URL state | Next.js search params | Active filters, sort order, pagination offset |

### API Client

A thin `fetch` wrapper in `lib/api/client.ts` that:
- Prepends the API base URL (`/api/v1`)
- Attaches `Authorization: Bearer <token>` header
- Intercepts 401 → triggers token refresh
- Includes `X-Trace-Id` header if provided
- Parses standard error envelope and throws typed errors

---

## Real-Time Connectivity

WebSocket connections for live updates. Connection managed in `lib/ws/`.

### Connection Lifecycle

1. After login, establish WS connection to relevant channels based on current page
2. Send auth message with access token on connect
3. On `auth_ok`, subscribe to page-specific streams
4. On `auth_error` or disconnect, attempt reconnect with exponential backoff (max 5 attempts)
5. On page navigation, close unused channels and open new ones

### Active Channels

| Channel | Path | Used By |
|---------|------|---------|
| Validation progress | `/spoke/common/data/{urn}/stream/validation` | DE, DA — validation detail page |
| Metric updates | `/spoke/dg/metric/stream` | DG — metrics dashboard |

Toast notifications surface WebSocket events that match user's notification preferences.
