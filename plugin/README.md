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
| `dataspoke-ontogen` | Manage Ontology Generation (UC3): singleton conf, Markdown seeds, dry-run + inference runs, result/event inspection, and node → edge → triple review. |
| `dataspoke-metagen` | Manage Metadata Generation (UC4): named confs, dataset opt-in boundaries, dry-run + generation runs, coverage, candidates, and global mutable review. |
| `dataspoke-governance` | Manage Governance metrics (UC5): define active metrics, dry-run + evaluate, and inspect trends, scoped datasets, and events. |

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
dataspoke-api --confirm PUT '/spoke/common/data/<urn>/attr/validation/conf' @/tmp/conf.json
```

Any method other than `GET` requires `--confirm` as the first argument, or the call is
refused and the method/URL/body are printed for review instead — a mechanical gate, not a
convention. A body may be a literal JSON string or `@PATH` to read it from a file (required for
anything multi-line, e.g. a Markdown seed body).

The validation skill may also use `bin/datahub-graphql` to search DataHub for dataset URNs. It
accepts one JSON object with a string `query` member. Queries run normally; a GraphQL mutation is
refused unless `--confirm` is its first argument. Its `@PATH` form is confined to readable,
non-symlink regular files outside known credential locations, and is preferred for multi-line
documents:

```text
datahub-graphql @/tmp/dataset-search.json
datahub-graphql --confirm @/tmp/approved-mutation.json
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
threshold or rule evaluation inside DataSpoke. A conf carries four sections: `description` and
`variables` declare what the pipeline will report; `attribute` states the dataset's data-arrival
cadence, read back by the governance `validation-score` metric; `parameter` is optional opaque
storage for the pipeline's own hyperparameters. Your pipeline computes every number, including
the pass/fail `score`, and POSTs `{data_time, score, variables}`. DataSpoke stores the history
and emits the result to DataHub as an assertion.

That division of labor is the point: `dataspoke-validation` writes the computing code — metrics,
baseline comparison, anomaly logic, thresholds — into *your* pipeline (or, for a reusable check,
your own shared package), where it runs on your engine with your credentials. It touches
DataSpoke only to **register** and, once it knows which utility implements the check,
**annotate** the conf's `description` at setup, **get** the recent baseline, and **post** each
run's result.
