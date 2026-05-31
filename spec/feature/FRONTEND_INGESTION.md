# Frontend — Ingestion Control (UC1)

> Companion to [FRONTEND_BASIC.md](FRONTEND_BASIC.md) (shell, navigation, shared
> components, polling model) and [API.md](../API.md) (routes). This page covers the
> Ingestion Control feature UI.

Ingestion is presented **per source / recipe** (one source produces many datasets). The UI's
two jobs mirror the feature's goals: manage sources (DataHub-compatible recipes) and make all
ingestion visible — which datasets each source covers, and which are ingested unmanaged.

## Routes

| Path | Purpose | API |
|------|---------|-----|
| `/ingestion` | Source list (filter by `mode`) | `GET /spoke/ingestion/sources` |
| `/ingestion/sources/new` | Create a source (`ACTIVE_CUSTOM_MANAGED` / `PASSIVE`) | `POST /spoke/ingestion/sources` |
| `/ingestion/sources/[id]` | Source detail (recipe, datasets, runs, events) | `GET /spoke/ingestion/sources/{id}` |
| `/ingestion/unmanaged` | Datasets covered by no source | `GET /spoke/ingestion/unmanaged` |

The per-dataset reverse-lookup is shown on the **dataset page** (see FRONTEND_BASIC dataset
detail), not a route here.

## List View (`/ingestion`)

One row per source: `name`, `mode` badge (`DATAHUB_MANAGED` / `ACTIVE_CUSTOM_MANAGED` /
`PASSIVE`), `platform`, schedule, enabled state, covered-dataset count, and latest run status.
Filter by `mode`; paginate. A "Create source" button routes to `/ingestion/sources/new`.
`DATAHUB_MANAGED` rows carry a read-only badge. (`GET /spoke/ingestion/sources`.)

## Source Detail (`/ingestion/sources/[id]`)

Four sections, each bound to a route:

1. **Recipe** — the source JSON (`{mode, name, schedule, recipe}`, recipe-standard wording) is
   rendered/edited as **YAML, secrets masked** — the YAML view is a lossless transform of the
   JSON body
   (`GET /spoke/ingestion/sources/{id}`). For `ACTIVE_CUSTOM_MANAGED` / `PASSIVE`, editable via a
   YAML editor (`PUT`/`PATCH`) and removable (`DELETE`). For `DATAHUB_MANAGED` the YAML is
   read-only — edits are disabled with an explanatory note that DataHub is SSOT (the API returns
   `409 INGESTION_SOURCE_READONLY`).
2. **Datasets** — the source→dataset mapping table (`GET /spoke/ingestion/sources/{id}/datasets`),
   each row showing the dataset URN and `origin` (`matcher` / `emitted` / `pipeline_name`).
3. **Run** — `POST /spoke/ingestion/sources/{id}/method/run` with a `dry_run` toggle. Shown only
   for `ACTIVE_CUSTOM_MANAGED`; other modes show an explanatory disabled state (the run happens in
   DataHub or externally; the API returns `409 INGESTION_RUN_NOT_APPLICABLE`).
4. **Events** — `GET /spoke/ingestion/sources/{id}/event` history table, newest first, with
   `from`/`to` filters.

## Create View (`/ingestion/sources/new`)

A `mode` selector (`ACTIVE_CUSTOM_MANAGED` / `PASSIVE` — `DATAHUB_MANAGED` is not creatable, it
is synced from DataHub) plus a YAML recipe editor. `ACTIVE_CUSTOM_MANAGED` recipes carry a
`schedule` (validated to one of hourly/daily/weekly) and `${name__key}` secret references;
`PASSIVE` recipes carry only the declared allow/deny scope. Submits via
`POST /spoke/ingestion/sources`.

## Unmanaged View (`/ingestion/unmanaged`)

A table of DataHub datasets covered by no source (`GET /spoke/ingestion/unmanaged`), paginated.
This is the "what's being ingested in an unmanaged way?" answer; each row links to its dataset
page.

## Per-dataset reverse-lookup

On a dataset's detail page, an "Ingestion" panel shows the owning source (link to
`/ingestion/sources/[id]`), its `mode`, and the latest run — from
`GET /spoke/common/data/{urn}/attr/ingestion`. Per-dataset ingestion events come from
`GET /spoke/common/data/{urn}/event/ingestion`.

## Components

- `IngestionSourceList` — the source list with the `mode` filter.
- `RecipeYamlEditor` — YAML recipe view/editor; read-only for `DATAHUB_MANAGED`, secrets masked.
- `SourceDatasetTable` — the source→dataset mapping table.
- `IngestionRunPanel` — dry-run / run trigger with status (`ACTIVE_CUSTOM_MANAGED` only).
- `IngestionEventTable` — shared event table bound to `…/event`.
- `UnmanagedDatasetTable` — the unmanaged-bucket list.

The page consumes API routes verbatim (no invented endpoints) per
[FRONTEND_BASIC.md](FRONTEND_BASIC.md). All mutations require the editor role; readers see a
read-only view.
