# DataSpoke plugin (End-User AI Scaffold)

A Claude Code plugin for **data engineers using a deployed DataSpoke service**. It lets you point
Claude at your organization's DataSpoke, ask how things work, and drive the public API — manage
ingestion, manage validation, and **author validation routines into your own pipelines**.

It is the end-user counterpart to the in-repo developer scaffold (`.claude/`, which *builds*
DataSpoke). This plugin only ever talks to a deployment's **public API** (`/api/v1/{auth,spoke,hub}`,
`/ready`, `/redoc`) — never helm, kubectl, source, or the database. See `spec/PLUGIN.md` for the
full specification.

## Install

```text
/plugin marketplace add <org>/<repo>      # this repository is a single-plugin marketplace
/plugin install dataspoke@dataspoke       # <plugin>@<marketplace>
```

For local development against a checkout: `/plugin marketplace add ./` then
`/plugin install dataspoke@dataspoke`, or launch with `claude --plugin-dir ./plugin`.

## Quickstart

1. **Connect** — `/dataspoke:dataspoke-access` and give it your deployment URL plus either a
   `dsk_` API token or your login (it mints one). Access is stored in `~/.dataspoke/config.json`
   (`chmod 600`).
2. **Use a feature** — e.g. `/dataspoke:dataspoke-validation` to register a validation slot or
   generate a routine; `/dataspoke:dataspoke-ingestion` to manage sources.

## Skills

| Skill | What it does |
|-------|--------------|
| `dataspoke-access` | Connect to / verify a deployment; mint & store a `dsk_` token. **Run first.** |
| `dataspoke-ingestion` | Manage ingestion sources (UC1): list, create/edit, dry-run + run, check results. |
| `dataspoke-validation` | Manage validation (UC2) **and** author a validation routine into your pipeline (flagship). |
| `dataspoke-ontogen` / `-metagen` / `-governance` | Stubs (UC3/4/5): answer questions + basic reads; point at `/redoc`. |

## Credentials & security

- Authentication uses a long-lived `dsk_` API token (`Authorization: Bearer dsk_…`). Mint it with
  `dataspoke-access`; it is shown **once** at mint time.
- The token lives only in `~/.dataspoke/config.json` (mode 600) or env overrides
  (`DATASPOKE_API_URL`, `DATASPOKE_API_TOKEN`). Treat it like a password — never commit it.
- **Write** operations need an **Editor/Admin** role; a Reader token gets `403 READ_ONLY_ROLE`.

## How calls are made

Every skill calls the API through `bin/dataspoke-api`, a small curl wrapper that resolves the base
URL + token, attaches the bearer header, and maps auth errors (401/403/409/422) to actionable
messages — so credential handling lives in one place.

```text
dataspoke-api GET  /auth/me
dataspoke-api GET  /spoke/validation
dataspoke-api PUT  '/spoke/common/data/<urn>/attr/validation/conf' '{"description":"…","variables":[]}'
```

## A note on validation

DataSpoke validation is a **passive result store**: a conf declares only `{description,
variables[]}`. Your pipeline computes the metrics and the pass/fail `score`, then POSTs
`{data_time, score, variables}`. The `dataspoke-validation` routine mode generates that pipeline
code for you — DataSpoke stores and emits the result, it does not run thresholds or forecasts.
