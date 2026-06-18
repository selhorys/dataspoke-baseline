# Frontend — Ingestion Control (UC1)

> Companion to [FRONTEND_BASIC.md](FRONTEND_BASIC.md) (shell, navigation, shared
> components, polling model) and [API.md](../API.md) (routes). This page covers the
> Ingestion Control feature UI.

Ingestion is presented **per source / recipe** (one source produces many datasets). The UI's
two jobs mirror the feature's goals: manage sources (DataHub-compatible recipes) and make all
ingestion visible — which datasets each source covers, and which are ingested unmanaged.

Ingestion is a collapsible sidebar **group** with two submenus — `conf` (the source list) and
`unmanaged`. Navigation between the two is via the sidebar only; neither page carries an in-page
cross-link to the other.

## Routes

| Path | Purpose | API |
|------|---------|-----|
| `/ingestion` | 302 to `/ingestion/conf` | — |
| `/ingestion/conf` | Source list (filter by `mode`) | `GET /spoke/ingestion/sources` |
| `/ingestion/sources/new` | Create a source (`ACTIVE_CUSTOM_MANAGED` / `PASSIVE`) | `POST /spoke/ingestion/sources` |
| `/ingestion/sources/[id]` | Source detail (recipe, datasets, runs, events) | `GET /spoke/ingestion/sources/{id}` |
| `/ingestion/unmanaged` | Datasets covered by no source | `GET /spoke/ingestion/unmanaged` |
| `/ingestion/data/[urn]` | Per-dataset reverse-lookup (owning source, latest run, events) | `GET /spoke/common/data/{dataset_urn}/attr/ingestion`, `GET …/event/ingestion` |

The per-dataset reverse-lookup page mirrors the per-dataset pages of Validation
(`/validation/data/[urn]`) and MetaGen (`/metagen/data/[urn]`).

## List View (`/ingestion/conf`)

One row per source: `name`, `mode` badge (`DATAHUB_MANAGED` / `ACTIVE_CUSTOM_MANAGED` /
`PASSIVE`), `platform`, schedule, enabled state, covered-dataset count, and latest run status.
A "Create source" button routes to `/ingestion/sources/new`; paginate. The filter offers
ALL, DataHub-managed, Active, Passive — each maps to the `mode` query param on
`GET /spoke/ingestion/sources` (DataHub-managed = `mode=DATAHUB_MANAGED`). Internal DataHub CLI
wrapper sources never appear — the backend hides them from the list, so DataHub-managed shows only
regular sources. `DATAHUB_MANAGED` rows carry a read-only badge. The name cell shows the source's
`datahub_source_urn` as a gray
subtitle below the name for DataHub-managed rows; rows without a URN
(`ACTIVE_CUSTOM_MANAGED` / `PASSIVE`) show the name alone. (`GET /spoke/ingestion/sources`.) The
covered-dataset count and latest run status are not fields on the list payload — each is
client-derived via a per-source fan-out (`datasets?limit=1` and `event?limit=1`).

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
4. **Events** — `GET /spoke/ingestion/sources/{id}/event` history table, newest first, aggregating
   the source's own runs with those booked on its internal DataHub wrappers. A row whose `wrapper`
   flag is set carries a "wrapper" tag. A
   `datetime`-granularity [RangePicker](FRONTEND_BASIC.md#shared-component-notes) (presets Last
   1 day / 7 days / 2 weeks (default) / 4 weeks / 12 weeks, plus a custom calendar range) drives
   the `from`/`to` filters from its inclusive `{from, to}` pair. The `detail` cell shows the
   compact JSON truncated to ~30 characters and is click-to-expand into a pretty-printed JSON
   dialog.

## Create View (`/ingestion/sources/new`)

A `mode` selector (`ACTIVE_CUSTOM_MANAGED` / `PASSIVE` — `DATAHUB_MANAGED` is not creatable, it
is synced from DataHub) plus a YAML recipe editor. `ACTIVE_CUSTOM_MANAGED` recipes carry a
`schedule` (validated to one of hourly/daily/weekly) and `${name__key}` secret references;
`PASSIVE` recipes carry only the declared allow/deny scope. Submits via
`POST /spoke/ingestion/sources`.

A **secret reference helper** sits beside the editor for `ACTIVE_CUSTOM_MANAGED` recipes. It
lists the source-credential references available to recipes (`GET /spoke/ingestion/secrets` — one
`${name__key}` per `(secret, key)` under the `dataspoke-source-cred-` prefix; values are never
shown) so an author can pick a known reference, and a collapsible **authoring guide** explains
how to provision a new one. DataSpoke is reference-only — there is no secret-write endpoint — so
the guide is read-only instruction, not a form: it shows the
`kubectl create secret generic dataspoke-source-cred-<name> --from-literal=<key>=… -n <namespace>`
recipe (the `<namespace>` is the API pod's own namespace), states the `dataspoke-source-cred-`
name prefix as a security boundary, and gives the `${name__key}` syntax for referencing the new
key in the recipe. The reference list refreshes from `GET /spoke/ingestion/secrets`; the guide
calls no write route. This is the UI rendering of
[SECRET_RESOLUTION.md §Admin authoring guide](SECRET_RESOLUTION.md).

## Unmanaged View (`/ingestion/unmanaged`)

A plain paginated table of DataHub datasets covered by no source
(`GET /spoke/ingestion/unmanaged`). This is the "what's being ingested in an unmanaged way?"
answer; each row links to its dataset page. Reached via the sidebar `unmanaged` submenu.

## Per-dataset reverse-lookup (`/ingestion/data/[urn]`)

An "Ingestion" panel shows the owning source (link to `/ingestion/sources/[id]`), its
`mode`, and the latest run (spanning the source's own runs and internal-wrapper runs) — from
`GET /spoke/common/data/{dataset_urn}/attr/ingestion`.
When no source covers the dataset, the panel says so and links to `/ingestion/unmanaged`.
Below it, an events table shows per-dataset ingestion events from
`GET /spoke/common/data/{dataset_urn}/event/ingestion`, with a `datetime`
[RangePicker](FRONTEND_BASIC.md#shared-component-notes) driving the endpoint's
`from`/`to` filters; a row whose `wrapper` flag is set carries a "wrapper" tag, and the `detail`
cell shows the compact JSON truncated to ~30 characters and is click-to-expand into a
pretty-printed JSON dialog. The page is read-only.

## Components

- `IngestionSourceList` — the source list with the `mode` filter (wrappers are hidden by the backend).
- `RecipeYamlEditor` — YAML recipe view/editor; read-only for `DATAHUB_MANAGED`, secrets masked.
- `SourceDatasetTable` — the source→dataset mapping table.
- `IngestionRunPanel` — dry-run / run trigger with status (`ACTIVE_CUSTOM_MANAGED` only).
- `SecretRefHelper` — the available-references list (`GET /spoke/ingestion/secrets`) plus the
  read-only authoring guide (kubectl recipe, namespace, `dataspoke-source-cred-` prefix,
  `${name__key}` syntax) shown in the source editor.
- `IngestionEventTable` — shared event table bound to `…/event`, paired with a `datetime`
  [RangePicker](FRONTEND_BASIC.md#shared-component-notes) for the `from`/`to` window; renders a
  "wrapper" tag on rows whose `wrapper` flag is set, and its `detail` cell truncates the JSON to ~30
  characters and is click-to-expand into a pretty-printed JSON dialog.
- `UnmanagedDatasetTable` — the unmanaged-bucket list.

Every paged table on these pages — `IngestionSourceList`, `SourceDatasetTable`,
`IngestionEventTable`, `UnmanagedDatasetTable`, and the per-dataset ingestion-event table — uses
the shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) control (page-size selector
defaulting to 20, Prev/Next, numbered pages) bound to each endpoint's standard
`offset`/`limit`/`total_count` envelope; no per-page Prev/Next is hand-rolled.

The page consumes API routes verbatim (no invented endpoints) per
[FRONTEND_BASIC.md](FRONTEND_BASIC.md). All mutations require the editor role; readers see a
read-only view.
