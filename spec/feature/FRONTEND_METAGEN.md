# DataSpoke Frontend — Metadata Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

MetaGen hosts the singleton operational conf, a global cross-dataset queue
of pending items, the per-dataset boundary editor, and the per-item
candidate review.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/metagen` | Global page (conf + queue) | `/spoke/metagen/attr/conf`, `/spoke/metagen/item`, `/spoke/metagen/event` |
| `/metagen/data/[urn]` | Per-dataset boundary + items | `/spoke/common/data/{urn}/attr/metagen/{conf,item}`, `/event/metagen` |

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/metagen` | `GET /spoke/metagen/attr/conf`, `GET /spoke/metagen/item`, `GET /spoke/metagen/event` | `PUT/PATCH/DELETE /spoke/metagen/attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `result_limit`, `overwrite_pending`); `POST /spoke/metagen/method/run` (optional body `{dataset_urns?, dry_run?}`) |
| `/metagen/data/[urn]` | `GET .../attr/metagen/conf`, `GET .../attr/metagen/item`, `GET .../attr/metagen/item/{item_id}` (per-item candidates), `GET .../event/metagen` | `PUT/PATCH/DELETE .../attr/metagen/conf` (fields: `is_enabled`, `allowed[]`); `POST .../attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

The global page (`/metagen`) is the singleton conf editor plus a
cross-dataset queue of pending items (filterable by `dataset_urn`, `kind`,
`status`). The per-dataset page (`/metagen/data/[urn]`) shows boundary
(`is_enabled`, `allowed`) and the dataset's items grouped by kind. Each item
renders as a card with up to `result_limit` candidate sub-cards carrying
Approve / Reject buttons; the confirm dialog labels the destination DataHub
aspect. Finalized items collapse to a single approved row with sibling
`llm_approved` candidates shown as read-only history. Review semantics
(approve-supersedes-sibling, reject-only-on-llm-approved) are in
[API §Metadata Generation](../API.md#metadata-generation-spokemetagen).

```
┌──────────────────────────────────────────────────────┐
│  ← catalog.books             boundary: [edit]        │
│  is_enabled: ✓   allowed: [dataset.description,      │
│                            column.description]       │
├──────────────────────────────────────────────────────┤
│  dataset.description            status: pending      │
│   ┌────────────────────────────────────────────────┐ │
│   │ c1  conf 0.92        [Approve] [Reject]        │ │
│   │ "# Books\n\nMaster catalog of every title…"    │ │
│   └────────────────────────────────────────────────┘ │
│   ┌────────────────────────────────────────────────┐ │
│   │ c2  conf 0.88        [Approve] [Reject]        │ │
│   │ "# Catalog: Books\n\nThe authoritative…"       │ │
│   └────────────────────────────────────────────────┘ │
│   ┌────────────────────────────────────────────────┐ │
│   │ c3  conf 0.85        [Approve] [Reject]        │ │
│   │ "Books table — Imazon's primary title…"        │ │
│   └────────────────────────────────────────────────┘ │
│                                                      │
│  column.book_id.description     status: approved     │
│   ✓ approved by alice on 2026-05-12 (switchable)     │
│   (2 sibling candidates collapsed — expand to view)  │
└──────────────────────────────────────────────────────┘
        Detail (`/metagen/data/[urn]`)
```

Write actions — conf save, `Run`, boundary edits, `Approve`, `Reject` — are
rendered only when `role ∈ {Editor, Admin}`. Reader users see the conf
values, the item queue, and candidate text, with no action buttons.
