---
name: dataspoke-access
description: Connect this plugin to a deployed DataSpoke and verify access. Use to point Claude at a DataSpoke deployment (API base URL + dsk_ API token), mint a token from email/password, or check who you are and what role you hold. Prerequisite for every other dataspoke-* skill — run this first, or when calls start returning 401.
argument-hint: "[set | status]"
allowed-tools: Read, Write, Bash(dataspoke-api *), Bash(curl *), Bash(chmod *), Bash(mkdir *), Bash(python3 *), AskUserQuestion
---

## Purpose

Establish and inspect this plugin's access to a **deployed** DataSpoke service. All other
`dataspoke-*` skills read the access config this skill writes. It touches only the public API
(`/ready`, `/api/v1/auth/*`) — never cluster internals.

Access lives in `~/.dataspoke/config.json` (`chmod 600`):

```json
{
  "api_base_url": "https://dataspoke.example.com/api/v1",
  "token": "dsk_…",
  "redoc_url": "https://dataspoke.example.com/redoc",
  "ui_url": "https://dataspoke.example.com",
  "datahub_gms_url": "https://datahub.example.com/gms",
  "datahub_token": "…"
}
```

`datahub_gms_url` and `datahub_token` are **optional** — they are only needed for the validation
skill's DataHub URN search (the `datahub-graphql` helper), not for basic DataSpoke access.

Environment variables override the file when present: `DATASPOKE_API_URL`, `DATASPOKE_API_TOKEN`
(and, for the DataHub URN search, `DATAHUB_GMS_URL`, `DATAHUB_TOKEN`).

## Modes

Resolve the mode from the argument (`set` / `status`); if absent, run `status` when a config
already exists, otherwise `set`.

### `status` — verify current access

1. `dataspoke-api GET /ready` — deployment reachable. Read the body, not just the status: it
   never returns 503, so `status: "degraded"` with a `false` entry in `checks` (`datahub`,
   `postgres`, `redis`) still arrives as 200. Name the degraded dependency in the report.
2. `dataspoke-api GET /auth/me` — confirm identity and **effective role**.
3. Report: base URL, deployment status, account email, role (Reader / Editor / Admin), and the
   redoc + UI URLs from the config. If the config is missing or `/auth/me` returns 401, tell the
   user to run `set`.

### `set` — configure access

1. **Collect the base URL.** Ask the user for the deployment origin (e.g.
   `https://dataspoke.example.com`, or the dev `http://api.<INGRESS_IP>.nip.io`). Derive
   `redoc_url` = `<origin>/redoc`, `ui_url` = `<origin>`, `api_base_url` = `<origin>/api/v1`.

   Then probe the origin **before collecting any credential**, so a typo or an unreachable
   deployment surfaces as a clear message rather than a confusing failure mid-mint. `/ready`
   is public — no token needed:
   ```bash
   curl -sS --max-time 10 -w '\n%{http_code}\n' "<origin>/ready"
   ```
   A `200` with a `{"status": …, "checks": {…}}` body confirms a reachable DataSpoke. On a
   connection failure or a non-200 code, report what was tried and ask the user to correct the
   origin — do not proceed to step 2. `/ready` reports state and never returns 503, so read the
   body too: `status: "degraded"` (any `false` entry in `checks` — `datahub`, `postgres`,
   `redis`) still arrives as 200. Surface which dependency is down as a warning and let the user
   decide whether to continue; minting works while DataHub is degraded, but feature skills that
   need it will fail later.

2. **Obtain a token.** Ask whether the user will *paste an existing* `dsk_…` token or *mint a
   new one* from credentials.

   - **Paste**: take the `dsk_…` value directly.
   - **Mint** (two steps against the deployment):
     ```bash
     # 1) login -> short-lived access token
     curl -sS -X POST "<origin>/api/v1/auth/token" \
       -H "Content-Type: application/json" \
       -d '{"email":"<email>","password":"<password>"}'
     # → {"access_token":"…","expires_in":…}

     # 2) mint a long-lived dsk_ token (Authorization: Bearer <access_token>)
     curl -sS -X POST "<origin>/api/v1/auth/api-tokens" \
       -H "Authorization: Bearer <access_token>" \
       -H "Content-Type: application/json" \
       -d '{"name":"claude-code-plugin"}'
     # → {"token":"dsk_…","id":…,"name":…,"role_snapshot":…}  (token shown ONCE)
     ```
     Capture `token` from the second response immediately — it is never retrievable again.
     Never echo the password back; do not store it.

3. **Optionally collect DataHub access.** Ask whether the user wants to enable the validation
   skill's DataHub URN search. If yes, collect the DataHub GMS origin (e.g.
   `https://datahub.example.com/gms`) and a DataHub personal access token, to be written as
   `datahub_gms_url` and `datahub_token`. These are optional — skip them for basic DataSpoke
   access. Treat the DataHub token exactly like the `dsk_` token: never echo it back, never store
   it anywhere but the mode-600 config (or the env override).

4. **Write the config.** Restrict the directory **before** writing the file, so the token is
   never briefly readable by other local users during the write:
   ```bash
   mkdir -p ~/.dataspoke && chmod 700 ~/.dataspoke
   # write config.json with api_base_url, token, redoc_url, ui_url
   # (plus optional datahub_gms_url, datahub_token when DataHub URN search is enabled)
   chmod 600 ~/.dataspoke/config.json
   ```

5. **Verify** with `dataspoke-api GET /auth/me` and report the resolved role. Remind the user
   that **write** operations (ingestion source CRUD, validation conf/result writes) require an
   **Editor** or **Admin** role; a Reader token returns `403 READ_ONLY_ROLE`.

## Notes

- `redoc_url` is stored so skills can hand a **human** a browsable API reference. It renders in a
  browser and is not readable by fetching it — skills that need the contract themselves call
  `dataspoke-schema <path-fragment>`, which reads the same document as JSON.
- The `dsk_` prefix marks the token for leak detection — treat it like a password; it belongs
  only in `~/.dataspoke/config.json` (mode 600) or the env override, never in committed files.
- To rotate or revoke: mint a new token (re-run `set`) and revoke old ones via
  `dataspoke-api DELETE /auth/api-tokens/{id}` (list them with `GET /auth/api-tokens`).
