---
name: dataspoke-ingestion
description: Manage DataSpoke ingestion sources (UC1) on a deployed instance — list and inspect sources, create or edit ACTIVE_CUSTOM_MANAGED and PASSIVE sources, trigger dry-run and real extractor runs, and review run history, emitted datasets, and the unmanaged bucket. Use for any "register/check ingestion" or "is this dataset ingested" question. Answers questions and, on request, writes and fires the API calls.
argument-hint: "[question or action]"
allowed-tools: Read, Bash(dataspoke-api *), AskUserQuestion
---

## Purpose

Drive Ingestion Control (UC1) through the public API. Acts as a question-answerer about how
ingestion works and, when the user asks, an API writer/launcher. Every call goes through
`dataspoke-api`; if it reports no access, send the user to `/dataspoke:dataspoke-access` first.

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

## Operating rules

- **Always dry-run before a real run** on `ACTIVE_CUSTOM_MANAGED` sources, and show the user the
  dry-run result before firing the real run.
- **Confirm before any write** (create/update/delete/run): restate the source name, mode, and
  schedule, and get explicit agreement.
- Recipes reference credentials as `${secret_name__key}` placeholders — never inline a plaintext
  secret. Discover available refs with `GET /spoke/ingestion/secrets`.
- Surface conflict codes (`409 INGESTION_SOURCE_READONLY`, `409 INGESTION_RUNNING`,
  `409 INGESTION_RUN_NOT_APPLICABLE`) and `403 READ_ONLY_ROLE` verbatim — do not work around them.
- After a real run, poll `…/event` (and `…/datasets`) to confirm completion and emitted datasets;
  newly ingested datasets can lag a couple of minutes in DataHub search.
