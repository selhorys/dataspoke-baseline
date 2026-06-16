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
  "ui_url": "https://dataspoke.example.com"
}
```

Environment variables override the file when present: `DATASPOKE_API_URL`, `DATASPOKE_API_TOKEN`.

## Modes

Resolve the mode from the argument (`set` / `status`); if absent, run `status` when a config
already exists, otherwise `set`.

### `status` — verify current access

1. `dataspoke-api GET /ready` — deployment reachable and healthy.
2. `dataspoke-api GET /auth/me` — confirm identity and **effective role**.
3. Report: base URL, account email, role (Reader / Editor / Admin), and the redoc + UI URLs
   from the config. If the config is missing or `/auth/me` returns 401, tell the user to run
   `set`.

### `set` — configure access

1. **Collect the base URL.** Ask the user for the deployment origin (e.g.
   `https://dataspoke.example.com`, or the dev `http://api.<INGRESS_IP>.nip.io`). Derive
   `redoc_url` = `<origin>/redoc`, `ui_url` = `<origin>`, `api_base_url` = `<origin>/api/v1`.

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

3. **Write the config.** Restrict the directory **before** writing the file, so the token is
   never briefly readable by other local users during the write:
   ```bash
   mkdir -p ~/.dataspoke && chmod 700 ~/.dataspoke
   # write config.json with api_base_url, token, redoc_url, ui_url
   chmod 600 ~/.dataspoke/config.json
   ```

4. **Verify** with `dataspoke-api GET /auth/me` and report the resolved role. Remind the user
   that **write** operations (ingestion source CRUD, validation conf/result writes) require an
   **Editor** or **Admin** role; a Reader token returns `403 READ_ONLY_ROLE`.

## Notes

- The `dsk_` prefix marks the token for leak detection — treat it like a password; it belongs
  only in `~/.dataspoke/config.json` (mode 600) or the env override, never in committed files.
- To rotate or revoke: mint a new token (re-run `set`) and revoke old ones via
  `dataspoke-api DELETE /auth/api-tokens/{id}` (list them with `GET /auth/api-tokens`).
