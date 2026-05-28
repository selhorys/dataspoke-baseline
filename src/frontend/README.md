# DataSpoke Frontend

Reference UI for the DataSpoke API. Built with Next.js 15 App Router, TypeScript, Tailwind CSS, TanStack Query, and Zustand.

## Running locally

```bash
pnpm install
pnpm dev          # starts on http://localhost:3000
```

The dev server proxies nothing. Configure the API base and DataHub URL in `.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=http://app.<INGRESS_IP>.nip.io
NEXT_PUBLIC_DATAHUB_URL=http://datahub.<INGRESS_IP>.nip.io
```

When `NEXT_PUBLIC_API_BASE_URL` is empty (the default), all `/api/v1/...` calls go to the same origin, which works when Next.js is served behind the same nginx-ingress as the API.

`NEXT_PUBLIC_DATAHUB_URL` enables the "Configure ingestion in DataHub" deep link on the passive-mode ingestion detail page. When unset, the link is suppressed but the page remains fully functional.

## Production / runtime configuration

The production image is built once and configured at runtime via environment variables injected by the Kubernetes ConfigMap. The root layout (a Next.js Server Component) reads these variables on every request and injects them into the page before any client bundle executes:

| Variable | Purpose |
|---|---|
| `DATASPOKE_API_BASE_URL` | Base URL of the DataSpoke API, e.g. `https://api.dataspoke.example.com` |
| `DATASPOKE_DATAHUB_URL` | Base URL of DataHub, e.g. `https://datahub.dataspoke.example.com` |

These are **non-public** server-side env vars (no `NEXT_PUBLIC_` prefix) so Next.js never inlines them at build time. The client reads them from `window.__DATASPOKE_RUNTIME_CONFIG__`, which is written by the inline script tag in `<head>`. The `NEXT_PUBLIC_*` vars remain supported as a dev-only fallback (`.env.local`) and are ignored in production when the runtime vars are set.

## Regenerating API types

Once the backend is running, regenerate TypeScript types from the live OpenAPI schema:

```bash
pnpm codegen
# or point at a non-default URL:
API_OPENAPI_URL=http://app.<INGRESS_IP>.nip.io/api/v1/openapi.json pnpm codegen
```

This writes `lib/api/types.generated.ts` using `openapi-typescript`. That file is git-ignored. The hand-written stubs in `lib/api/types.ts` remain the source of truth until the generated file is available.
