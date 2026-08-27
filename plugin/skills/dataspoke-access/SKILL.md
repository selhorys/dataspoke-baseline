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

Resolve the mode from the argument (`set` / `status`); if absent, run `status` when access is
already resolvable — a config file exists, **or** both `DATASPOKE_API_URL` and
`DATASPOKE_API_TOKEN` are set in the environment (the env-only path step 4 below describes) —
otherwise `set`.

### `status` — verify current access

1. `dataspoke-api GET /ready` — deployment reachable. Read the body, not just the status: it
   never returns 503, so `status: "degraded"` with a `false` entry in `checks` (`datahub`,
   `postgres`, `redis`) still arrives as 200. Name the degraded dependency in the report.
2. `dataspoke-api GET /auth/me` — confirm identity and the **account's** current role. This is
   *not* the token's effective privilege (see the `role_snapshot` note below) — it's the account
   row, so it can read Admin while the token in use still only acts as a Reader.
3. Report: base URL, deployment status, account email, role (Reader / Editor / Admin — the
   account's, per the caveat above), and the redoc + UI URLs from the config. If the config is
   missing or `/auth/me` returns 401, tell the user to run `set`.

### `set` — configure access

Both first-time setup and re-configuration (rotating the `dsk_` token, adding DataHub access
later) go through this mode — `dataspoke-validation` and `datahub-graphql` both send users here
to *add* DataHub credentials, and the Notes below make re-running `set` the token-rotation path.
A re-run must only change what the user actually wants to change, not silently drop the rest —
`dsk_` tokens cannot be listed back out (minted once, never retrievable) and are capped at 10
active per account (`409 TOKEN_LIMIT_EXCEEDED`), so a user rotating the DataSpoke token but
declining step 3 must keep their existing `datahub_gms_url`/`datahub_token`.

**This has to be done value-blind — never by reading the stored config into the agent's
context.** A run that only wants to add DataHub credentials touches neither the password nor
the `dsk_`/DataHub tokens in that run; reading the existing file to "carry forward" what it
already holds would pull both secrets into the conversation and this session's transcript for
no reason, defeating the whole point of step 2's out-of-band mint recipe. Instead, check what's
already configured **by key presence only**, never by value. Keep this (and every embedded
`python3 -c` command below) as a single logical line — a multi-line quoted Python body picks up
this list's own indentation as literal leading whitespace on each line, which is a Python
`IndentationError` waiting to happen the moment it's actually run:
```bash
python3 -c "import json,os; p=os.path.expanduser('~/.dataspoke/config.json'); cfg=json.load(open(p)) if os.path.exists(p) else {}; print(json.dumps({k: bool(cfg.get(k)) for k in ('api_base_url','token','redoc_url','ui_url','datahub_gms_url','datahub_token')}))"
```
Use this to decide which of steps 1-3 to ask about (e.g. offer "keep existing" for a token that's
already present, and mention DataHub is already configured rather than re-asking blind) — never
to fetch the values themselves. Step 5's write is a merge performed entirely inside `python3`,
which is the only thing that ever touches the old values; they're never printed, logged, or
handed back to the agent.

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

2. **Obtain a token.** If the presence check above shows a `token` already configured and this
   run isn't specifically about rotating it, ask whether to *keep the token already configured*,
   *paste* a different existing `dsk_…` token, or *mint a new one* from credentials.

   - **Keep existing**: simplest case — collect nothing here. Leave the `token` field out of
     what step 5 sets, and its merge leaves the stored value untouched without ever reading it.
   - **Paste**: take the `dsk_…` value directly. Never type or echo it into a command line —
     write it straight into `config.json` (step 5) rather than passing it through a shell
     argument first.
   - **Mint**. The account password must never be typed into a message to the agent, and never
     land in a command line the agent runs: an agent's `Bash` tool call is non-interactive (a
     password prompt has no terminal to read from — it would either hang or silently read empty
     input and mint against an empty password), and anything the user types into the
     conversation itself becomes part of the transcript the model provider holds, which is
     exactly the exposure this recipe exists to avoid. **The agent does not run this step.**
     Hand the user the two-command recipe below and have them run it themselves, **in a `bash`
     shell specifically** — it relies on `read -rs`'s `-s` (silent) flag, which is not POSIX;
     under `/bin/sh`/`dash` it would be rejected or ignored, either echoing the password or
     reading empty input and minting against an empty password — then report back only the
     resulting `dsk_…` token:
     ```bash
     # 1) login -> short-lived access token. Prompts for the password (not echoed) and
     #    never places it on a command line, in shell history, or in the process table.
     printf 'DataSpoke account password: ' >&2; read -rs DS_PASSWORD; echo
     DS_EMAIL="<email>" DS_PASSWORD="$DS_PASSWORD" python3 -c "import json,os; print(json.dumps({'email': os.environ['DS_EMAIL'], 'password': os.environ['DS_PASSWORD']}))" \
       | curl -sS -X POST "<origin>/api/v1/auth/token" \
           -H "Content-Type: application/json" -d @-
     unset DS_PASSWORD
     # → {"access_token":"…","expires_in":…}

     # 2) mint a long-lived dsk_ token. The access token from step 1 is itself a bearer
     #    credential (it can mint dsk_ tokens), so acquire it the same way as the password —
     #    never as a literal shell assignment, which would land in shell history — and send it
     #    to curl the same way too, via stdin config rather than a -H argv literal:
     printf 'access_token from step 1 (paste, not echoed): ' >&2; read -rs DS_ACCESS; echo
     printf 'header = "Authorization: Bearer %s"\n' "$DS_ACCESS" \
       | curl -sS -K - -X POST "<origin>/api/v1/auth/api-tokens" \
           -H "Content-Type: application/json" -d '{"name":"claude-code-plugin"}'
     unset DS_ACCESS
     # → {"token":"dsk_…","id":…,"name":…,"role_snapshot":…}  (token shown ONCE)
     ```
     The user reports back the `token` value from the second response — never the password,
     never the access token — and only that value ever reaches the conversation. Once captured,
     treat the `dsk_…` token the same way going forward: pass it to config-writing code through
     a variable or file, never as a literal in a command line.

     `role_snapshot` (returned verbatim by this call and by `GET /auth/api-tokens`) records the
     role the account held *at mint time* and never changes afterward. The token's actual
     privilege — its **effective role** — is the lower of `role_snapshot` and the account's
     *current* role: promoting the account never upgrades an already-minted token (only a fresh
     mint picks up the higher role), while demoting the account immediately lowers what existing
     tokens can do. No route returns the effective role directly — `GET /auth/me` reports only
     the account's current role (see `status` above), not this intersection — so the only signal
     available is behavioral: a `403` on a write despite `/auth/me` showing Editor/Admin means
     the token itself is stale, and retrying will not help; mint a fresh one.

3. **Optionally collect DataHub access.** Ask whether the user wants to enable the validation
   skill's DataHub URN search. If yes, collect the DataHub GMS origin (e.g.
   `https://datahub.example.com/gms`) and a DataHub personal access token, to be written as
   `datahub_gms_url` and `datahub_token`. These are optional — skip them for basic DataSpoke
   access. Treat the DataHub token exactly like the `dsk_` token: never echo it back, never store
   it anywhere but the mode-600 config (or the env override).

4. **Disclose what the config file means, before writing it.** `~/.dataspoke/config.json` is the
   right mechanism available to a skill — there is no keychain facility here — but the user
   should hear what that implies before the first write, not discover it later:
   - The token is stored **unencrypted**, protected only by filesystem permissions (`chmod 600`).
   - A backup tool or a cloud-synced home directory (Dropbox, iCloud Drive, a dotfiles repo)
     will copy it as plaintext.
   - It is a bearer credential: anyone who obtains it can act as the user until it is revoked.
   - **Deleting the local file is not the same as revoking the credential.** Deleting
     `~/.dataspoke/config.json` only stops *this machine* from using the token — the token
     itself stays valid until explicitly revoked server-side
     (`dataspoke-api --confirm DELETE /auth/api-tokens/{id}`, see Notes below). If the file may
     have been copied off this machine (a cloud sync, a backup), revocation is the only thing
     that actually invalidates it — deleting the local copy alone is not enough.
   - The token also reaches wherever this conversation reaches: capturing it at mint time,
     pasting it, or writing it to config all put it in the model's context and this
     conversation's transcript, held by the model provider, not just on disk. If the token must
     never enter the conversation, mint from the user's own terminal (see step 2 above) and
     export both `DATASPOKE_API_URL` and `DATASPOKE_API_TOKEN` instead of writing this config —
     `dataspoke-api` needs both to resolve a base URL (`TOKEN` alone with no config file is not
     enough), and this skill's own `status` mode won't have a `redoc_url`/`ui_url` to report
     without the config file, since there's no env override for those.
   - That env-var override also avoids writing anything to disk at all, at the cost of only
     lasting the shell session.

5. **Write the config — a value-blind merge, not an overwrite.** `python3` loads the existing
   file (or starts from `{}`), sets only the keys this run actually collected — passed in via
   environment variables, never argv, and only for fields this run touched — and writes the
   result back. A key with no `DS_SET_*` variable set this run is left byte-for-byte as it was;
   the agent never reads the old value to "carry it forward", so `datahub_gms_url`/
   `datahub_token` — and a kept-existing `token` — survive untouched without ever entering
   context. Restrict the directory **before** writing, so the file is never briefly readable by
   other local users during the write:
   ```bash
   mkdir -p ~/.dataspoke && chmod 700 ~/.dataspoke
   # Set ONLY the DS_SET_* vars for fields this run actually collected — e.g. skip
   # DS_SET_TOKEN entirely on "keep existing", skip the DATAHUB ones if step 3 wasn't run.
   DS_SET_API_BASE_URL="<from step 1>" \
   DS_SET_REDOC_URL="<from step 1>" \
   DS_SET_UI_URL="<from step 1>" \
   python3 -c "import json,os; p=os.path.expanduser('~/.dataspoke/config.json'); cfg=json.load(open(p)) if os.path.exists(p) else {}; pairs=[('api_base_url','DS_SET_API_BASE_URL'),('token','DS_SET_TOKEN'),('redoc_url','DS_SET_REDOC_URL'),('ui_url','DS_SET_UI_URL'),('datahub_gms_url','DS_SET_DATAHUB_GMS_URL'),('datahub_token','DS_SET_DATAHUB_TOKEN')]; cfg.update({k: os.environ[e] for k,e in pairs if e in os.environ}); json.dump(cfg, open(p,'w'))"
   chmod 600 ~/.dataspoke/config.json
   ```
   Add `DS_SET_TOKEN=...` to the env-var prefix only when this run pasted or minted a token;
   add `DS_SET_DATAHUB_GMS_URL=...`/`DS_SET_DATAHUB_TOKEN=...` only when step 3 collected them.

6. **Verify** with `dataspoke-api GET /auth/me` and report the account's current role (not the
   token's effective role — see the `role_snapshot` note above). Remind the user that **write**
   operations (ingestion source CRUD, validation conf/result writes) require an **Editor** or
   **Admin** role; a Reader token returns `403 READ_ONLY_ROLE`.

## Notes

- `redoc_url` is stored so skills can hand a **human** a browsable API reference. It renders in a
  browser and is not readable by fetching it — skills that need the contract themselves call
  `dataspoke-schema <path-fragment>`, which reads the same document as JSON.
- The `dsk_` prefix marks the token for leak detection — treat it like a password; it belongs
  only in `~/.dataspoke/config.json` (mode 600) or the env override, never in committed files.
- To rotate or revoke: mint a new token (re-run `set`) and revoke old ones via
  `dataspoke-api --confirm DELETE /auth/api-tokens/{id}` (list them with `GET /auth/api-tokens`).
