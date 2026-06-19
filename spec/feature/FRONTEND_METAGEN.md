# DataSpoke Frontend — Metadata Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

MetaGen manages a **collection** of generation confs, surfaces one global
cross-dataset review queue, lists datasets no conf documents, and hosts the
per-dataset boundary editor + per-item candidate review.

MetaGen is a collapsible sidebar **group** with three submenus — `conf` (the
conf list and per-conf editors/runs), `result` (the global review queue +
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
| `/metagen/conf/[id]` | Conf detail (fields, run, per-conf events) | `GET /spoke/metagen/conf/{conf_id}` |
| `/metagen/result` | Global cross-dataset review queue + cross-conf events | `GET /spoke/metagen/item`, `GET /spoke/metagen/event` |
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
| `/metagen/conf/[id]` | `GET /spoke/metagen/conf/{conf_id}`, `GET /spoke/metagen/conf/{conf_id}/event` | `PUT/PATCH /spoke/metagen/conf/{conf_id}`; `DELETE /spoke/metagen/conf/{conf_id}`; `POST /spoke/metagen/conf/{conf_id}/method/run` (optional body `{dataset_urns?}`; `?dry_run=true`) |
| `/metagen/result` | `GET /spoke/metagen/item`, `GET /spoke/metagen/event` | — (review happens on the MetaGen panel of `/data/[urn]`) |
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
deletes the conf (button → ConfirmDialog → `DELETE`; the dialog notes that
already-approved descriptions stay in DataHub while this conf's pending
candidates are dropped), and triggers a run with a `dry_run` toggle
(`POST /spoke/metagen/conf/{conf_id}/method/run`; concurrent run returns
`409 METAGEN_RUNNING`, disabled non-dry-run returns `409 METAGEN_DISABLED`).
Below the form an events table shows this conf's run history
(`GET /spoke/metagen/conf/{conf_id}/event`), newest first, with a `datetime`
[RangePicker](FRONTEND_BASIC.md#shared-component-notes) driving `from`/`to`.

## Result queue (`/metagen/result`)

A paginated cross-dataset, cross-conf queue of items (filterable by
`dataset_urn` text, `kind`, `status`, and a `conf_id` select), rendering
`field_path`, `status`, and a `candidate_count` column
(`GET /spoke/metagen/item`). An item aggregates candidates from one or more
confs, so there is no single producing conf per row; `conf_id`/`conf_name`
surface per-candidate at the item detail (see [Per-dataset](#per-dataset-dataurn-metagen-panel)),
and the `conf_id` filter narrows the queue to items holding a candidate from
that conf. Each row links to the owning dataset page
(`/data/[urn]`) where the candidate review happens. A second tab/section
shows the cross-conf event feed (`GET /spoke/metagen/event`) with a `datetime`
[RangePicker](FRONTEND_BASIC.md#shared-component-notes).

## Uncovered (`/metagen/uncovered`)

A paginated table of registered datasets reached by no conf
(`GET /spoke/metagen/uncovered`), each row carrying a `reason`
(`no_conf_match` / `boundary_blocked`). An `include_disallowed` toggle maps to
the `?include_disallowed` query param: off (default) shows only `no_conf_match`
rows; on additionally shows `boundary_blocked` rows. This is the metagen
analogue of the ingestion `/ingestion/unmanaged` view. Each row links to its
dataset page. Read-only.

## Per-dataset (`/data/[urn]` MetaGen panel)

The MetaGen panel on the unified [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page
shows the boundary (`is_enabled`, `allowed`, `owner`) over
`attr/metagen/boundary` and the dataset's items grouped by kind. The header
renders an enabled/disabled badge beside the dataset URN. Each item renders as
a card with its candidate sub-cards (carrying the producing `conf_name`) and
Approve / Reject buttons; the confirm dialog labels the destination DataHub
aspect. Finalized items collapse to a single approved row; sibling candidates —
including ones from other confs — are shown as collapsed history and remain
selectable. Approving a sibling switches the approved candidate, demoting the
prior approval (possibly from a different conf) to `llm_approved`. Review
semantics (approve-supersedes-sibling-across-confs, reject-only-on-llm-approved)
are in [API §Metadata Generation](../API.md#metadata-generation-spokemetagen).

```
┌──────────────────────────────────────────────────────┐
│  ← catalog.books             boundary: [edit]        │
│  is_enabled: ✓   allowed: [dataset.description,      │
│                            column.description]       │
├──────────────────────────────────────────────────────┤
│  dataset.description            status: pending      │
│   ┌────────────────────────────────────────────────┐ │
│   │ c1  conf 0.92  [fulfillment]  [Approve][Reject]│ │
│   │ "# Books\n\nMaster catalog of every title…"    │ │
│   └────────────────────────────────────────────────┘ │
│   ┌────────────────────────────────────────────────┐ │
│   │ c2  conf 0.88  [eu-privacy]   [Approve][Reject]│ │
│   │ "# Catalog: Books\n\nThe authoritative…"       │ │
│   └────────────────────────────────────────────────┘ │
│                                                      │
│  column.book_id.description     status: approved     │
│   ✓ approved by alice on 2026-05-12 (switchable)     │
│   (sibling candidates collapsed — expand to view)    │
└──────────────────────────────────────────────────────┘
   MetaGen panel on `/data/[urn]`
```

Write actions — conf save / delete / `Run` (on `/metagen/conf/*`), boundary
edits, `Approve`, `Reject` (on the MetaGen panel of `/data/[urn]`) — render only when
`role ∈ {Editor, Admin}`. Reader users see conf values, the item queue, the
uncovered list, and candidate text, with no action buttons.

## Components

- `MetagenConfList` — the conf list with the "Create conf" button.
- `MetagenConfForm` — the conf form (create + edit), with the `dataset_filter` builder.
- `RunDialog` — per-conf dry-run / run trigger dialog with status.
- `QueueTable` — the global cross-dataset/cross-conf item queue with `conf_id` filter.
- `MetagenUncoveredTable` — the uncovered-datasets list with the `include_disallowed` toggle.
- `BoundaryForm` — the per-dataset boundary form (`attr/metagen/boundary`).
- `ItemCard` — per-item card holding the candidate sub-cards with Approve / Reject and the `conf_name` tag.
- `CandidateCard` — a single candidate sub-card (value, `confidence_score`, `conf_name` tag, Approve / Reject) rendered inside `ItemCard`.
- `MetagenEventTable` — shared event table bound to a `…/event` route (conf-detail + cross-conf
  feeds), paired with a `datetime` [RangePicker](FRONTEND_BASIC.md#shared-component-notes) for the
  `from`/`to` window.
- `MetagenDataPanel` — the per-dataset boundary form + item/candidate review, composed by the
  unified [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page (its metagen events show
  in that page's unified Events panel).

Every paged table here — `MetagenConfList`, `QueueTable`, `MetagenUncoveredTable`, and
`MetagenEventTable` (conf-detail + cross-conf feeds) — uses the shared
[Pagination](FRONTEND_BASIC.md#shared-component-notes) control (page-size selector defaulting to
20, Prev/Next, numbered pages) bound to each endpoint's standard
`offset`/`limit`/`total_count` envelope; no per-page Prev/Next is hand-rolled.

The page consumes API routes verbatim (no invented endpoints) per
[FRONTEND_BASIC.md](FRONTEND_BASIC.md). All mutations require the editor role;
readers see a read-only view.
