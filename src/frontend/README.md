# DataSpoke Frontend

Reference UI for the DataSpoke API. Built with Next.js 15 App Router, TypeScript, Tailwind CSS, TanStack Query, and Zustand.

## Running locally

```bash
pnpm install
pnpm dev          # starts on http://localhost:3000
```

The dev server proxies nothing. Configure the API base and the Airflow URL in `.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=http://api.<INGRESS_IP>.nip.io
NEXT_PUBLIC_AIRFLOW_URL=http://airflow.<INGRESS_IP>.nip.io
```

When `NEXT_PUBLIC_API_BASE_URL` is empty (the default), all `/api/v1/...` calls go to the same origin, which works when Next.js is served behind the same nginx-ingress as the API.

The DataHub and Langfuse links — the header infra icons, the "Configure ingestion in DataHub" deep link on the passive-mode ingestion detail page, the per-dataset DataHub links, and the Langfuse evidence links — are not configured here. They come from the DataHub / Langfuse peripheral config served by `GET /spoke/common/peripheral-links` and wired via `PATCH /api/v1/admin/peripherals/{datahub,langfuse}`. When a peripheral is unwired the corresponding link is suppressed and the page remains fully functional.

## Production / runtime configuration

The production image is built once and configured at runtime via environment variables injected by the Kubernetes ConfigMap. The root layout (a Next.js Server Component) reads these variables on every request and injects them into the page before any client bundle executes:

| Variable | Purpose |
|---|---|
| `DATASPOKE_API_BASE_URL` | Base URL of the DataSpoke API, e.g. `https://api.dataspoke.example.com` |
| `DATASPOKE_AIRFLOW_URL` | Base URL of the Airflow UI, e.g. `https://airflow.dataspoke.example.com` |

These are **non-public** server-side env vars (no `NEXT_PUBLIC_` prefix) so Next.js never inlines them at build time. Both rendering sides read them through `getRuntimeConfig()` in `lib/runtime-config.ts`:

- **Client** — from `window.__DATASPOKE_RUNTIME_CONFIG__`, written by the inline script tag in `<head>`.
- **Server** (SSR and Server Components, where that window global does not exist yet) — from `process.env` directly. Server-rendered markup therefore carries the deployed URLs, so absolute links such as the Google sign-in href are correct in the first HTML response rather than depending on hydration to repair them.

Resolution is per field, and an empty string counts as unset: each of `apiBaseUrl` and `airflowUrl` takes the `DATASPOKE_*` value when it is non-empty, otherwise the matching `NEXT_PUBLIC_*` value, otherwise `""`. The `NEXT_PUBLIC_*` vars are a dev-only fallback (`.env.local`); in production, where they are unset, the `DATASPOKE_*` values are what resolve.

Only deployment-local wiring travels this way: the API itself (which also backs the ReDoc link) and Airflow, both of which ship with the deployment. Externally-wired peripherals — DataHub and Langfuse — are configured through the API's peripheral endpoints instead, so re-wiring them needs no chart operation and no pod restart.

## Regenerating API types

Once the backend is running, regenerate TypeScript types from the live OpenAPI schema:

```bash
pnpm codegen
# or point at a non-default URL:
API_OPENAPI_URL=http://api.<INGRESS_IP>.nip.io/api/v1/openapi.json pnpm codegen
```

This writes `lib/api/types.generated.ts` using `openapi-typescript`. That file is git-ignored. The hand-written stubs in `lib/api/types.ts` remain the source of truth until the generated file is available.
