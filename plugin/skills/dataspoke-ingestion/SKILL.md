---
name: dataspoke-ingestion
description: Manage DataSpoke ingestion sources (UC1) on a deployed instance — list and inspect sources, create or edit ACTIVE_CUSTOM_MANAGED and PASSIVE sources, trigger dry-run and real extractor runs, and review run history, emitted datasets, and the unmanaged bucket. Use for any "register/check ingestion" or "is this dataset ingested" question. Answers questions and, on request, writes and fires the API calls.
argument-hint: "[question or action]"
allowed-tools: Read, Bash(dataspoke-api *), Bash(dataspoke-schema *), AskUserQuestion
---

## Purpose

Drive Ingestion Control (UC1) through the public API. Acts as a question-answerer about how
ingestion works and, when the user asks, an API writer/launcher. Every call goes through
`dataspoke-api`; if it reports no access, send the user to `/dataspoke:dataspoke-access` first.

## Read the contract before authoring a body

The tables below cover intent → route. For exact **request and response shapes** — field names,
required fields, enums, nested recipe structure — read the deployment's own OpenAPI rather than
guessing or working from memory:

```bash
dataspoke-schema ingestion/sources --list     # discover operations, one line each
dataspoke-schema ingestion/sources            # full schema for those operations
```

Do this before writing any `POST`/`PUT`/`PATCH` body — the source body is the most detailed shape
in the plugin, and `CreateIngestionSourceRequest` documents every field including the recipe
structure and the schedule tiers. Narrow the fragment to keep the output small
(`dataspoke-schema 'sources/{id}/method/run'`). `/redoc` is the same document rendered for
**humans** — hand that URL to the user; use `dataspoke-schema` yourself.

## Source modes

- **`DATAHUB_MANAGED`** — synced from DataHub; read-only here (cannot be authored/edited).
- **`ACTIVE_CUSTOM_MANAGED`** — DataSpoke-owned extractor with a recipe + schedule; runnable.
- **`PASSIVE`** — an external ingestor with a declared scope; observed, not run by DataSpoke.

## Capabilities → routes

| Intent | Call |
|--------|------|
| List sources (optionally `?mode=…`) | `dataspoke-api GET /spoke/ingestion/sources` |
| Inspect one source | `dataspoke-api GET /spoke/ingestion/sources/{id}` |
| Create a source | `dataspoke-api POST /spoke/ingestion/sources '<json>'` |
| Replace / update | `dataspoke-api PUT|PATCH /spoke/ingestion/sources/{id} '<json>'` |
| Delete a source | `dataspoke-api DELETE /spoke/ingestion/sources/{id}` |
| Dry-run (connection check, no writes) | `dataspoke-api POST '/spoke/ingestion/sources/{id}/method/run?dry_run=true' '{}'` |
| Real run | `dataspoke-api POST /spoke/ingestion/sources/{id}/method/run '{}'` |
| Run / event history | `dataspoke-api GET /spoke/ingestion/sources/{id}/event` |
| Datasets covered by a source | `dataspoke-api GET /spoke/ingestion/sources/{id}/datasets` |
| Datasets covered by no source | `dataspoke-api GET /spoke/ingestion/unmanaged` |
| Available credential refs (Editor+) | `dataspoke-api GET /spoke/ingestion/secrets` |
| Reverse lookup for a dataset | `dataspoke-api GET /spoke/common/data/{urn}/attr/ingestion` |

Every list route is paginated — **`limit` defaults to 20** (cap 1000), with `offset` and `sort`.
Never conclude "there are only N sources" from an unpaged first page; check `total_count`. The
`sources` list omits the internal DataHub CLI wrapper sources entirely, so `?mode=DATAHUB_MANAGED`
returns regular synced sources only.

Row shapes worth knowing: `…/datasets` rows carry `authority` + `derivation`; `…/event` rows
carry a derived `wrapper: bool` (`true` when the event originated on a linked internal wrapper
rather than the source itself); `/secrets` rows are `{ref, secret_name, key}` and **never**
include values. `/unmanaged` reads a registry refreshed hourly by the `datahub-sync-hourly`
sweep, so a just-ingested dataset can still appear there for up to an hour.

## Operating rules

- **Always dry-run before a real run** on `ACTIVE_CUSTOM_MANAGED` sources, and show the user the
  dry-run result before firing the real run.
- **Confirm before any write** (create/update/delete/run): restate the source name, mode, and
  schedule, and get explicit agreement. `DELETE` also cascades the source's dataset mappings.
- Recipes reference credentials as `${secret_name__key}` placeholders — never inline a plaintext
  secret. Discover available refs with `GET /spoke/ingestion/secrets`. A malformed reference is
  rejected at create/update time with `422 SECRET_REF_MALFORMED`. DataSpoke has no secret-write
  API: an Admin authors the K8s Secret out-of-band
  (`kubectl create secret generic dataspoke-source-cred-<name> --from-literal=<key>=… -n <ns>`),
  and the recipe only ever holds the reference.
- Surface conflict codes (`409 INGESTION_SOURCE_READONLY`, `409 INGESTION_RUNNING`,
  `409 INGESTION_RUN_NOT_APPLICABLE`) and `403 READ_ONLY_ROLE` verbatim — do not work around them.
- After a real run, poll `…/event` (and `…/datasets`) to confirm completion and emitted datasets;
  newly ingested datasets can lag a couple of minutes in DataHub search.

## Reading a run report

The run response carries `run_id` and `status` at the top level, plus a `detail` object — and the
matching `INGESTION.COMPLETE` / `INGESTION.FAIL` event `detail` carries the same fields (plus
`run_id` and `platform`). Interpret it rather than dumping it:

| Field | Meaning |
|-------|---------|
| `dry_run` | Whether this was a preview |
| `discovered_urns` / `_count` | Datasets passing the filter — the "would emit" plan. Present on **both** dry-run and real runs |
| `emitted_urns` / `_count` | Datasets actually written to DataHub. Empty with count `0` on a dry-run |
| `errors`, `warnings` | Per-run diagnostics |

A dry-run connects, crawls `information_schema`, and applies `schema_pattern` — so
`discovered_urns` is the concrete list to show the user before a real run. `emitted_urns ⊆
discovered_urns` always. On a **real** run, `discovered_urns_count − emitted_urns_count > 0`
means per-table emit failures: report it as a partial success and point at `errors`, never as a
clean run.
