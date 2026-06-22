# DataSpoke Frontend — Metadata Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

MetaGen manages a **collection** of generation confs, surfaces a per-dataset
result rollup, lists datasets no conf documents, and hosts the
per-dataset boundary editor + per-item candidate review.

MetaGen is a collapsible sidebar **group** with three submenus — `conf` (the
conf list and per-conf editors/runs), `result` (the per-dataset result rollup +
events), and `uncovered`. Navigation between them is via the sidebar only;
neither page carries an in-page cross-link to the others. This mirrors the
group/submenu pattern of [FRONTEND_INGESTION.md](FRONTEND_INGESTION.md).

---

## Routes

| Path | Purpose | API |
|------|---------|-----|
| `/metagen` | 302 to `/metagen/conf` | — |
| `/metagen/conf` | Conf list (create / edit / run) | `GET /spoke/metagen/conf` |
| `/metagen/conf/new` | Create a conf | `POST /spoke/metagen/conf` |
| `/metagen/conf/[id]` | Conf detail (fields, run, covered datasets, per-conf events) | `GET /spoke/metagen/conf/{conf_id}`, `GET /spoke/metagen/conf/{conf_id}/dataset` |
| `/metagen/result` | Per-dataset result rollup + cross-conf events | `GET /spoke/metagen/dataset`, `GET /spoke/metagen/event` |
| `/metagen/uncovered` | Datasets reached by no conf | `GET /spoke/metagen/uncovered` |
| `/metagen/data/[urn]` | Redirect to the unified `/data/[urn]` page (deep-link preserved) | — |

The per-dataset metagen detail (boundary + items + candidate review) lives as the **MetaGen**
panel on the unified [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page; the
dataset's metagen events fold into that page's unified **Events** panel.

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/metagen/conf` | `GET /spoke/metagen/conf` | — (a "Create conf" button routes to `/metagen/conf/new`) |
| `/metagen/conf/new` | — | `POST /spoke/metagen/conf` (fields: `name`, `is_enabled`, `schedule_tier`, `dataset_filter`, `result_limit`, `overwrite_pending`) |
| `/metagen/conf/[id]` | `GET /spoke/metagen/conf/{conf_id}`, `GET /spoke/metagen/conf/{conf_id}/dataset` (with `include_disallowed` toggle), `GET /spoke/metagen/conf/{conf_id}/event` | `PUT/PATCH /spoke/metagen/conf/{conf_id}`; `DELETE /spoke/metagen/conf/{conf_id}`; `POST /spoke/metagen/conf/{conf_id}/method/run` (optional body `{dataset_urns?}`; `?dry_run=true`) |
| `/metagen/result` | `GET /spoke/metagen/dataset`, `GET /spoke/metagen/event` | — (review happens on the MetaGen panel of `/data/[urn]`) |
| `/metagen/uncovered` | `GET /spoke/metagen/uncovered` (with `include_disallowed` toggle) | — |
| `/data/[urn]` MetaGen panel | `GET …/attr/metagen/boundary`, `GET …/attr/metagen/item`, `GET …/attr/metagen/item/{item_id}` (per-item candidates) | `PUT/PATCH/DELETE …/attr/metagen/boundary` (fields: `is_enabled`, `allowed[]`, `owner`); `POST …/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

## Conf list (`/metagen/conf`)

One row per conf: `name`, `is_enabled` badge, `schedule_tier`, a
`dataset_filter` summary, and `result_limit`. A "Create conf" button routes to
`/metagen/conf/new`; paginate (`GET /spoke/metagen/conf`). Each row links to
`/metagen/conf/[id]`.

## Conf create / detail (`/metagen/conf/new`, `/metagen/conf/[id]`)

The create page is a form over `{name, is_enabled, schedule_tier,
dataset_filter, result_limit, overwrite_pending}`, submitting via
`POST /spoke/metagen/conf`; a duplicate `name` surfaces `409 METAGEN_CONF_EXISTS`.

The detail page edits the same fields (`PUT` full replace / `PATCH` partial),
deletes the conf (button → ConfirmDialog → `DELETE`; the dialog notes that this
conf's generated items and candidates are retained as parentless results while
already-approved descriptions stay in DataHub), and triggers a run with a `dry_run` toggle
(`POST /spoke/metagen/conf/{conf_id}/method/run`; concurrent run returns
`409 METAGEN_RUNNING`, disabled non-dry-run returns `409 METAGEN_DISABLED`).

Below the form a paginated **Covered datasets** table
(`GET /spoke/metagen/conf/{conf_id}/dataset`) lists the datasets this conf's
`dataset_filter` matches. Each row renders `dataset_urn` (linking to
`/data/[urn]`) and a boundary-setting summary (an `is_enabled` badge plus an
`allowed` summary). A read-only "Show boundary-blocked" toggle maps to the
`?include_disallowed` query param: off (default) shows only writable covered
datasets; on additionally surfaces boundary-blocked covered rows (each carrying
its `blocked`/`reason`). The table sits **above** the events panel.

Below the Covered datasets table an events table shows this conf's run history
(`GET /spoke/metagen/conf/{conf_id}/event`), newest first, with a `datetime`
[RangePicker](FRONTEND_BASIC.md#shared-component-notes) driving `from`/`to`. Each
row's detail cell truncates by default and expands to pretty-printed JSON on
click.

## Result rollup (`/metagen/result`)

A paginated **per-dataset rollup** of generation results — one row per dataset,
not per item (`GET /spoke/metagen/dataset`). Columns:

| Column | Source |
|---|---|
| dataset / boundary | `dataset_urn` (links to `/data/[urn]`) on the first line; the boundary `allowed` labels as badges (or "none" when empty) on the second |
| items | `item_count` |
| approved | `approved_count` |
| rejected | `rejected_count` |
| candidates | `candidate_count` |
| last modified at | `last_modified_at` (formatted in the session tz) |

Counts are candidate-level. Two filters sit below the table: a `dataset_urn`
text input and a `conf_id` select (from `GET /spoke/metagen/conf`). Setting
`conf_id` restricts rows to datasets holding a candidate from that conf and
scopes every count to that conf's candidates. There are no `kind` or `status`
filters on this surface. Each row links to the owning dataset page
(`/data/[urn]`) where the candidate review happens.

A second tab/section shows the cross-conf event feed
(`GET /spoke/metagen/event`) with a `datetime`
[RangePicker](FRONTEND_BASIC.md#shared-component-notes).

## Uncovered (`/metagen/uncovered`)

A paginated table of registered datasets reached by no conf
(`GET /spoke/metagen/uncovered`), each row carrying a `reason`
(`no_conf_match` / `boundary_blocked`). A "Show boundary-blocked datasets"
toggle maps to the `?include_disallowed` query param: off (default) shows only `no_conf_match`
rows; on additionally shows `boundary_blocked` rows. This is the metagen
analogue of the ingestion `/ingestion/unmanaged` view. Each row links to its
dataset page. Read-only.

## Per-dataset (`/data/[urn]` MetaGen panel)

The MetaGen panel on the unified [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page
shows the boundary (`is_enabled`, `allowed`, `owner`) over
`attr/metagen/boundary`, then the dataset's candidates in **two foldable panels —
one per item kind**: `dataset.description` and `column.description`. The header
renders an enabled/disabled badge beside the dataset URN. Each panel holds a
single **table whose rows are candidates** (fetched per item via
`GET …/attr/metagen/item/{item_id}`). Common columns:

| Column | Source |
|---|---|
| generated value | `candidate.value`, rendered multi-line (`<pre>`) |
| run info | producing `conf_name` (muted "no conf" when null because the conf was deleted) · `confidence_score` · an **Evidence** link to the Langfuse trace built from `run_id` |
| status | `candidate.status` badge (`llm_approved` / `approved` / `rejected`) |
| action | `Approve` / `Reject` keyed to that row's `(dataset_urn, item_id, candidate_id)` |

The `dataset.description` panel normally has one item (one column group). The
`column.description` panel's table carries a **leading `field_path` column** and
**groups rows by column** (= item): each column's candidates render contiguously
(per-column sub-header or `rowSpan`) so the approve action's scope — and its
sibling-demotion effect — is visible per column.

Approving a candidate demotes the prior approved sibling in the **same item /
column** (possibly from a different conf) to `llm_approved` — not a reject. The
confirm dialog states this; the `Reject` action's dialog labels the destination
DataHub aspect, and on an `approved` candidate notes that the editable DataHub
description it wrote will be removed. After a review the panel refetches the
item(s) so the demoted sibling's status updates in place. Review semantics
(approve-supersedes-sibling-across-confs with demote-to-`llm_approved`; reject
valid on both `llm_approved` and `approved`, with the approved case removing the
DataHub description) are in
[API §Metadata Generation](../API.md#metadata-generation-spokemetagen).

The boundary section's action controls live in its section header's top-right
cluster, mirroring the [Validation panel's header-right action pattern](FRONTEND_VALIDATION.md):
read mode (a boundary exists) shows `Edit`; create / edit mode shows
`Save boundary`, plus `Cancel` when editing an existing boundary. All map to
`PUT .../attr/metagen/boundary`.

```
┌────────────────────────────────────────────────────────────────┐
│  ← catalog.books                            boundary  [Edit]    │
│  is_enabled: ✓   allowed: [dataset.description,                │
│                            column.description]                 │
├────────────────────────────────────────────────────────────────┤
│  ▾ dataset.description                                          │
│   ┌──────────────────────┬───────────────────┬────────┬───────┐│
│   │ generated value      │ run info          │ status │ action││
│   ├──────────────────────┼───────────────────┼────────┼───────┤│
│   │ # Books\nMaster cat… │ fulfillment 0.92 ↗│ llm_app│ [✓][✗]││
│   │ # Catalog: Books\n…  │ eu-privacy 0.88 ↗ │ approve│ [✗]   ││
│   └──────────────────────┴───────────────────┴────────┴───────┘│
├────────────────────────────────────────────────────────────────┤
│  ▾ column.description                                           │
│   ┌──────────┬───────────────┬────────────────┬────────┬──────┐│
│   │ field    │ generated val │ run info       │ status │action││
│   ├──────────┼───────────────┼────────────────┼────────┼──────┤│
│   │ book_id  │ Surrogate key │ fulfillment ↗  │ approve│ [✗]  ││
│   │          │ Primary id…   │ eu-privacy ↗   │ llm_app│[✓][✗]││
│   ├──────────┼───────────────┼────────────────┼────────┼──────┤│
│   │ title    │ Book title…   │ fulfillment ↗  │ llm_app│[✓][✗]││
│   └──────────┴───────────────┴────────────────┴────────┴──────┘│
└────────────────────────────────────────────────────────────────┘
   MetaGen panel on `/data/[urn]` — per-kind foldable tables,
   column.description grouped by field_path
```

Write actions — conf save / delete / `Run` (on `/metagen/conf/*`), boundary
edits, `Approve`, `Reject` (on the MetaGen panel of `/data/[urn]`) — render only when
`role ∈ {Editor, Admin}`. Reader users see conf values, the result rollup, the
uncovered list, and candidate text, with no action buttons.

## Components

- `MetagenConfList` — the conf list with the "Create conf" button.
- `MetagenConfForm` — the conf form (create + edit), with the `dataset_filter` builder.
- `RunDialog` — per-conf dry-run / run trigger dialog with status.
- `MetagenDatasetTable` — the per-dataset result rollup on `/metagen/result` (`GET /spoke/metagen/dataset`) with the `dataset_urn` text filter and `conf_id` select.
- `MetagenUncoveredTable` — the uncovered-datasets list with the `include_disallowed` toggle.
- `BoundaryForm` — the per-dataset boundary form (`attr/metagen/boundary`).
- `ItemKindTable` — one per item kind on the `/data/[urn]` MetaGen panel, inside a foldable panel: a candidate-row table (generated value, run info, status, Approve / Reject). The `column.description` instance adds a leading `field_path` column and groups its rows by column (item). Each row's `Approve` / `Reject` is keyed to that row's `(dataset_urn, item_id, candidate_id)`.
- `MetagenCoveredTable` — the per-conf covered-datasets list with the "Show boundary-blocked" toggle (`?include_disallowed`), on `/metagen/conf/[id]` above `MetagenEventTable`.
- `MetagenEventTable` — shared event table bound to a `…/event` route (conf-detail + cross-conf
  feeds), paired with a `datetime` [RangePicker](FRONTEND_BASIC.md#shared-component-notes) for the
  `from`/`to` window. The detail cell truncates with click-to-expand pretty-JSON.
- `MetagenDataPanel` — the per-dataset boundary form + item/candidate review, composed by the
  unified [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page (its metagen events show
  in that page's unified Events panel).

Every paged table here — `MetagenConfList`, `MetagenDatasetTable`, `MetagenUncoveredTable`,
`MetagenCoveredTable`, and `MetagenEventTable` (conf-detail + cross-conf feeds) — uses the shared
[Pagination](FRONTEND_BASIC.md#shared-component-notes) control (page-size selector defaulting to
20, Prev/Next, numbered pages) bound to each endpoint's standard
`offset`/`limit`/`total_count` envelope; no per-page Prev/Next is hand-rolled.

The page consumes API routes verbatim (no invented endpoints) per
[FRONTEND_BASIC.md](FRONTEND_BASIC.md). All mutations require the editor role;
readers see a read-only view.
