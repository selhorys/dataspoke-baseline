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
| `/ingestion/data/[urn]` | Redirect to the unified per-dataset page `/data/[urn]` (deep-link preserved) | — |

The per-dataset detail lives at the unified **`/data/[urn]`** page (see
[FRONTEND_BASIC §Per-dataset page](FRONTEND_BASIC.md#per-dataset-page-dataurn)). The
reverse-lookup display (owning source, mode, latest-run time and status) is the
**Ingestion summary card** there — there is no separate Ingestion foldable panel; the
dataset's ingestion events fold into that page's unified **Events** panel
(`GET /spoke/common/data/{dataset_urn}/event` with `event_major_type=INGESTION`).

## List View (`/ingestion/conf`)

One row per source: `name`, `mode` badge (`DATAHUB_MANAGED` / `ACTIVE_CUSTOM_MANAGED` /
`PASSIVE`), `platform`, schedule, covered-dataset count, and latest run status. All three
`mode` badges share **one neutral style** (no per-mode color). The schedule cell renders a
tier (hourly / daily / weekly) as a link to its backing Airflow DAG (`ingestion-active-<tier>`)
**only for `ACTIVE_CUSTOM_MANAGED`**; for `DATAHUB_MANAGED` and `PASSIVE` it renders plain text
`delegated` (no Airflow link), since their scheduling lives in DataHub or outside DataSpoke. An
unscheduled / custom-cron active value renders as plain text.
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
`platform`, `status`, `datahub_source_urn`, the `mode` badge (the shared neutral style), and
the schedule. `datahub_source_urn` renders as a link (new tab) to the DataHub ingestion-sources
list `<datahubUrl>/ingestion/sources?hideSystem=true` — a list deep-link, not a per-source page —
falling back to plain text when `datahubUrl` is unset. The schedule links its tier (hourly /
daily / weekly) to the backing Airflow DAG
(`ingestion-active-<tier>`) **only for `ACTIVE_CUSTOM_MANAGED`**; for `DATAHUB_MANAGED` and
`PASSIVE` it renders plain text `delegated`. Below it, four sections, each bound to a route:

1. **Recipe** — the source JSON (`{mode, name, schedule, recipe}`, recipe-standard wording) is
   rendered/edited as **YAML, secrets masked** — the YAML view is a lossless transform of the
   JSON body
   (`GET /spoke/ingestion/sources/{id}`). The read-only `<pre>` view caps its height at ~25
   lines and scrolls (`overflow-auto`) for longer recipes; the edit-mode textarea is unbounded. For `ACTIVE_CUSTOM_MANAGED` / `PASSIVE`, editable via a
   YAML editor and removable (`DELETE`). Save wires `PUT` (full replace); partial recipe
   edits use `PATCH /sources/{id}`. View-mode shows `Edit` / `Delete` at the section header's
   top-right; edit-mode replaces them with `Save` / `Cancel` in that same slot (the editor's own
   bottom actions are suppressed), and surfaces the collapsible `SecretRefAuthoringGuide` under
   the editor's secret-ref line for `ACTIVE_CUSTOM_MANAGED` sources. For `DATAHUB_MANAGED` the
   YAML is read-only — edits are disabled with an explanatory note that DataHub is SSOT (the API
   returns `409 INGESTION_SOURCE_READONLY`).
2. **Datasets** — the source→dataset mapping table (`GET /spoke/ingestion/sources/{id}/datasets`).
   Its second column is `datahub` — the shared
   [DataHub dataset deep-link](FRONTEND_BASIC.md#shared-component-notes) (`<datahubUrl>/dataset/{urn}`,
   rendered only when `datahubUrl` is set). The table also carries a single `authority` column whose
   cell fuses both server fields, rendered as e.g. `high (emitted)`: the dataset URN, its `authority`
   (`high` / `medium`) and `derivation` (`emitted` / `pipeline_name` / `matched`). `authority` is
   derived from `derivation` — `emitted` / `pipeline_name` ⇒ `high`, `matched` ⇒ `medium`.
3. **Run** — `POST /spoke/ingestion/sources/{id}/method/run` with a `dry_run` toggle. Shown only
   for `ACTIVE_CUSTOM_MANAGED`; other modes show an explanatory disabled state (the run happens in
   DataHub or externally; the API returns `409 INGESTION_RUN_NOT_APPLICABLE`). For `DATAHUB_MANAGED`
   and `PASSIVE`, a second line below the read-only explanation briefly describes the
   `datahub-sync-hourly` DAG's role (it refreshes the source→dataset mapping and mirrors run status
   on a fixed hourly cadence) with an Airflow link to `<airflowUrl>/dags/datahub-sync-hourly`
   (rendered only when `airflowUrl` is set).
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

The page places `Cancel` (→ `/ingestion/conf`) and `Save` at the **top-right**, beside the
title — mirroring the source-detail edit-mode wiring. `Save` submits the recipe form
externally and the editor's own bottom save is suppressed.

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
answer; each row links to its dataset page, and its second column is `datahub` — the shared
[DataHub dataset deep-link](FRONTEND_BASIC.md#shared-component-notes) (rendered only when
`datahubUrl` is set). Reached via the sidebar `unmanaged` submenu.

## Per-dataset reverse-lookup (unified `/data/[urn]`)

The reverse-lookup display lives in the **Ingestion summary card** on the unified per-dataset
page (see [FRONTEND_BASIC §Per-dataset page](FRONTEND_BASIC.md#per-dataset-page-dataurn)) — there
is no separate Ingestion foldable panel. The card shows the owning source (link to
`/ingestion/sources/[id]`), its `mode`, and the latest run's **time** and status (spanning the
source's own runs and internal-wrapper runs) — from
`GET /spoke/common/data/{dataset_urn}/attr/ingestion`. When no source covers the dataset, the
card says so and links to `/ingestion/unmanaged`. The display is read-only. The dataset's
ingestion events are not a separate table here — they appear in the page's unified **Events**
panel (narrow with `event_major_type=INGESTION`); wrapper-origin rows carry a "wrapper" tag.

## Components

- `IngestionSourceList` — the source list with the `mode` filter (wrappers are hidden by the backend).
- `RecipeYamlEditor` — YAML recipe view/editor; read-only for `DATAHUB_MANAGED`, secrets masked.
- `SourceDatasetTable` — the source→dataset mapping table.
- `IngestionRunPanel` — dry-run / run trigger with status (`ACTIVE_CUSTOM_MANAGED` only).
- `SecretRefHelper` — the available-references list (`GET /spoke/ingestion/secrets`) plus the
  `SecretRefAuthoringGuide`, shown in the Create page.
- `SecretRefAuthoringGuide` — the collapsible read-only authoring guide (kubectl recipe,
  namespace, `dataspoke-source-cred-` prefix, `${name__key}` syntax); reused by `SecretRefHelper`
  and the source-detail recipe editor.
- `IngestionEventTable` — shared event table bound to the per-source `…/sources/{id}/event`,
  paired with a `datetime` [RangePicker](FRONTEND_BASIC.md#shared-component-notes) for the
  `from`/`to` window; renders a "wrapper" tag on rows whose `wrapper` flag is set, and its `detail`
  cell truncates the JSON to ~30 characters and is click-to-expand into a pretty-printed JSON dialog.
- `IngestionSummaryCard` — the per-dataset reverse-lookup display (owning source link / mode /
  latest-run time and status), composed by the unified
  [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page.
- `UnmanagedDatasetTable` — the unmanaged-bucket list, with a `datahub` deep-link second column.

Every paged table on these pages — `IngestionSourceList`, `SourceDatasetTable`,
`IngestionEventTable`, and `UnmanagedDatasetTable` — uses
the shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) control (page-size selector
defaulting to 20, Prev/Next, numbered pages) bound to each endpoint's standard
`offset`/`limit`/`total_count` envelope; no per-page Prev/Next is hand-rolled.

The page consumes API routes verbatim (no invented endpoints) per
[FRONTEND_BASIC.md](FRONTEND_BASIC.md). All mutations require the editor role; readers see a
read-only view.
