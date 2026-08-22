---
name: e2e-cross-host-cookie-auth
description: E2E storageState auth depends on a fragile cross-host refresh-cookie chain — require a green smoke run on a real cluster before trusting it
metadata:
  type: project
---

The Playwright E2E harness (`tests/e2e/`) authenticates by saving a per-role `storageState` in `global-setup` and reusing it; it never re-logs-in per test. This works ONLY through a non-obvious cross-host cookie chain that is real-but-unproven in code review:

- The page loads from `app.<domain>` but the token refresh (`POST /api/v1/auth/token/refresh`, `credentials:"include"`) is cross-origin to `api.<domain>`.
- On load, `SilentRefresh` (`src/frontend/app/providers.tsx`) → `ensureFreshToken()` (`src/frontend/lib/api/client.ts`) fires the refresh; `AuthGuard` waits on `authInitialized` then checks the in-memory `accessToken`.
- The refresh token is an HttpOnly cookie set with `SameSite=Lax`, host-only, `path=/api/v1/auth/token` (`src/api/routers/auth.py`), and the API sets CORS `allow_credentials=True` (`src/api/main.py`).
- The chain holds only if: `nip.io` subdomains stay same-site (so Lax sends the cookie on the cross-origin XHR), the cookie path-prefix matches the refresh route, `cookie_secure` is not forcing HTTPS-only over the http dev ingress, and Playwright replays the cross-domain cookie from storageState.

**Why:** If any link breaks (e.g. `cookie_secure=true`, domain stops being same-site), every role test silently lands on `/login` and the entire E2E suite fails at the harness level — not a test bug. Code review cannot prove the chain; only a live run can.

**How to apply:** When reviewing E2E scaffold or new UC/ground specs, do NOT approve the storageState auth approach on code inspection alone. Require evidence that `tests/e2e/use-case/_smoke.spec.ts` (the harness self-test: admin storageState renders an authenticated-only element, NOT `/login`) ran green against a real `--frontend cluster` deploy. Treat an unrun smoke gate as a PARTIAL on T4 for any auth-dependent E2E test. Related: separately, a non-discriminating locator like `getByRole("main")` does not prove authentication because the public `/login` layout also renders `<main>` — insist on an authenticated-only locator (e.g. AppShell nav link).
