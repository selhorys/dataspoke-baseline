# DataSpoke E2E Tests

Playwright/TypeScript end-to-end tests for the full stack (browser → API → DataHub / PostgreSQL).

## Prerequisites

1. Full dev stack running with cluster frontend:
   `./helm-charts/bin/install.sh --profile dev --frontend cluster`
2. Health check passes: `./helm-charts/bin/health-check.sh`
3. Playwright Chromium installed: `pnpm -C tests/e2e exec playwright install chromium`
4. `helm-charts/.env.dev` exported: `set -a && source helm-charts/.env.dev && set +a`

## Run commands

```bash
# Install dependencies
pnpm -C tests/e2e install

# Type check (no cluster required)
pnpm -C tests/e2e typecheck

# List tests (no cluster required)
pnpm -C tests/e2e exec playwright test --list

# Run all E2E tests (requires deployed cluster + cluster frontend)
pnpm -C tests/e2e test

# Run headed (useful for debugging)
pnpm -C tests/e2e test:headed

# Open Playwright UI
pnpm -C tests/e2e test:ui
```

## Layout

```
tests/e2e/
├── use-case/          # uc{1..5}-<slug>.spec.ts — one browser flow per USE_CASE_en.md story
├── ground/<feature>/  # narrow per-page UI-flow tests (spot analogue)
├── fixtures/
│   ├── env.ts         # helm-charts/.env.dev loader + typed accessors (single source of truth)
│   └── index.ts       # custom Playwright fixtures (adminApi, toggleStub, IMAZON_URNS)
├── global-setup.ts    # lock + reset-seed + per-role login → .auth/*.json
├── global-teardown.ts # reset-seed + lock release
└── playwright.config.ts
```

## Auth and lock/reset

**Lock**: global-setup acquires the dev-env lock at `http://<INGRESS_IP>:9221/lock/acquire`
(same lock as Python integration tests). Set `DATASPOKE_DEV_LOCK_PREACQUIRED=1` if an outer
process holds it.

**Reset**: global-setup calls `uv run python -m tests.integration.util --reset-seed`. The Python
utilities are reused — no TS reimplementation.

**Auth**: global-setup logs in once per role (admin / editor / reader) through the real `/login`
page and saves `storageState` JSON to `.auth/`. The refresh token is an HttpOnly cookie; the app
restores the in-memory access token on page load via `POST /auth/token/refresh` (see
`src/frontend/app/providers.tsx` `SilentRefresh`). Playwright projects are keyed on role.

## Two groups

- `use-case/` — one browser flow per `USE_CASE_en.md` story (mirrors `api_wired/`)
- `ground/` — narrow single-concern UI flows per feature page (mirrors `spot/`)

Real-LLM variants (UC3/UC4): first `PATCH /api/v1/admin/conf {"stub_llm_client": false}`, then
run; revert with `{"stub_llm_client": true}` afterward.
