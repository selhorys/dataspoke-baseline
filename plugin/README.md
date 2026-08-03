# DataSpoke plugin (End-User AI Scaffold)

A Claude Code plugin for **data engineers using a deployed DataSpoke service**. It lets you point
Claude at your organization's DataSpoke, ask how things work, and drive the public API — manage
ingestion, manage validation, and **write data-quality validation into the pipelines you are
building**, with DataSpoke as the store for the scores and their history.

It is the end-user counterpart to the in-repo developer scaffold (`.claude/`, which *builds*
DataSpoke). This plugin only ever talks to a deployment's **public API** (`/api/v1/{auth,spoke}`,
`/ready`, `/openapi.json`, `/redoc`) — never helm, kubectl, source, or the database. See
`spec/AI_PLUGIN.md` for the full specification.

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
2. **Use a feature** — e.g. `/dataspoke:dataspoke-validation` while writing a pipeline, to get
   validation code for the partition it just wrote; `/dataspoke:dataspoke-ingestion` to manage
   sources.

## Skills

| Skill | What it does |
|-------|--------------|
| `dataspoke-access` | Connect to / verify a deployment; mint & store a `dsk_` token. **Run first.** |
| `dataspoke-ingestion` | Manage ingestion sources (UC1): list, create/edit, dry-run + run, check results. |
| `dataspoke-validation` | Write validation into your pipeline (UC2, flagship) — metrics, baseline, scoring, and the DataSpoke calls. Also manages validation slots directly. |
| `dataspoke-ontogen` / `-metagen` / `-governance` | Stubs (UC3/4/5): answer questions + basic reads; point at `/redoc`. |

## Credentials & security

- Authentication uses a long-lived `dsk_` API token (`Authorization: Bearer dsk_…`). Mint it with
  `dataspoke-access`; it is shown **once** at mint time.
- The token lives only in `~/.dataspoke/config.json` (mode 600) or env overrides
  (`DATASPOKE_API_URL`, `DATASPOKE_API_TOKEN`). Treat it like a password — never commit it.
- **Write** operations need an **Editor/Admin** role; a Reader token gets `403 READ_ONLY_ROLE`.

## How calls are made

Every skill calls the API through `bin/dataspoke-api`, a small curl wrapper that resolves the base
URL + token, attaches the bearer header, and maps error codes (401/403/404/409/422) to actionable
messages — so credential handling lives in one place.

```text
dataspoke-api GET  /auth/me
dataspoke-api GET  /spoke/validation
dataspoke-api PUT  '/spoke/common/data/<urn>/attr/validation/conf' '{"description":"…","variables":[]}'
```

## Reading the API contract

The deployment is the authority on its own API. `bin/dataspoke-schema` reads its `/openapi.json`
and prints only the operations matching a path fragment, plus every schema they reference — so a
skill can check the real request/response shape without pulling the whole contract into context
(the full document is ~220 KB; a narrowed lookup is 5–40 KB).

```text
dataspoke-schema ingestion/sources --list    # one line per operation: method, path, summary
dataspoke-schema ingestion/sources           # those operations + their resolved schemas
```

`/redoc` is the same document rendered **for humans** to browse — its URL is stored as
`redoc_url`. It is a browser page, not a readable source: skills use `dataspoke-schema`.

## A note on validation

DataSpoke validation is an **API for registration, get, and put of values** — it ships no
computing engine. There is no metric computation, no forecasting, no anomaly detection, and no
threshold or rule evaluation inside DataSpoke. A conf declares only `{description, variables[]}`;
your pipeline computes every number, including the pass/fail `score`, and POSTs
`{data_time, score, variables}`. DataSpoke stores the history and emits the result to DataHub as
an assertion.

That division of labor is the point: `dataspoke-validation` writes the computing code — metrics,
baseline comparison, anomaly logic, thresholds — into *your* pipeline, where it runs on your
engine with your credentials, and touches DataSpoke only to **register** the slot once, **get**
the recent baseline, and **put** each run's result.
