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
| `/ingestion/data/[urn]` | Per-dataset reverse-lookup (owning source, latest run, events) | `GET /spoke/common/data/{dataset_urn}/attr/ingestion`, `GET …/event/ingestion` |

The per-dataset reverse-lookup page mirrors the per-dataset pages of Validation
(`/validation/data/[urn]`) and MetaGen (`/metagen/data/[urn]`).

## List View (`/ingestion`)

One row per source: `name`, `mode` badge (`DATAHUB_MANAGED` / `ACTIVE_CUSTOM_MANAGED` /
`PASSIVE`), `platform`, schedule, enabled state, covered-dataset count, and latest run status.
Filter by `mode`; paginate. A "Create source" button routes to `/ingestion/sources/new`.
`DATAHUB_MANAGED` rows carry a read-only badge. (`GET /spoke/ingestion/sources`.) The covered-dataset
count and latest run status are not fields on the list payload — each is client-derived via a
per-source fan-out (`datasets?limit=1` and `event?limit=1`).

## Source Detail (`/ingestion/sources/[id]`)

A header surfaces read-only management fields as badges/text outside the recipe YAML section:
`platform`, `status`, and `datahub_source_urn`. Below it, four sections, each bound to a route:

1. **Recipe** — the source JSON (`{mode, name, schedule, recipe}`, recipe-standard wording) is
   rendered/edited as **YAML, secrets masked** — the YAML view is a lossless transform of the
   JSON body
   (`GET /spoke/ingestion/sources/{id}`). For `ACTIVE_CUSTOM_MANAGED` / `PASSIVE`, editable via a
   YAML editor and removable (`DELETE`). Save wires `PUT` (full replace); there is no `PATCH`
   recipe-edit surface. For `DATAHUB_MANAGED` the YAML is
   read-only — edits are disabled with an explanatory note that DataHub is SSOT (the API returns
   `409 INGESTION_SOURCE_READONLY`).
2. **Datasets** — the source→dataset mapping table (`GET /spoke/ingestion/sources/{id}/datasets`).
   The table carries a single `authority` column whose cell fuses both server fields, rendered as
   e.g. `high (emitted)`: the dataset URN, its `authority` (`high` / `medium`) and `derivation`
   (`emitted` / `pipeline_name` / `matched`). `authority` is derived from `derivation` —
   `emitted` ⇒ `high`, `matched` ⇒ `medium`.
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

## Per-dataset reverse-lookup (`/ingestion/data/[urn]`)

An "Ingestion" panel shows the owning source (link to `/ingestion/sources/[id]`), its
`mode`, and the latest run — from `GET /spoke/common/data/{dataset_urn}/attr/ingestion`.
When no source covers the dataset, the panel says so and links to `/ingestion/unmanaged`.
Below it, an events table shows per-dataset ingestion events from
`GET /spoke/common/data/{dataset_urn}/event/ingestion`. The page is read-only.

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
