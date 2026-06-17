# DataSpoke Frontend — Ontology Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

OntoGen hosts the singleton operational conf, the Markdown seed library,
and the triple-ontology browser with inline review. Approval lives here —
inline within each per-type panel and surfaced visually in the Graph view —
gated by Editor / Admin role.

---

## Navigation

The OntoGen sidebar entry is a foldable group with three children —
**conf · seed · result**. `/ontogen` redirects to `/ontogen/result`.

| UI route | Title | API base |
|---|---|---|
| `/ontogen/result` | Browser + review | `/spoke/ontogen/result/{node\|edge\|triple}/...` |
| `/ontogen/conf` | Conf editor + Run | `/spoke/ontogen/attr/conf`, `/spoke/ontogen/method/run` |
| `/ontogen/seed` | Seed library | `/spoke/ontogen/attr/seed/...` |

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/ontogen/conf` | `GET /spoke/ontogen/attr/conf` | `PUT/PATCH .../attr/conf` (Edit/Save/Cancel; fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) — all action controls sit top-right; the conf is a singleton so the UI exposes no Delete. `POST .../method/run` via Run dialog (optional Markdown body — one-shot prompt; "Dry run — evaluate without persisting results" checkbox → `?dry_run=true`) |
| `/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}` |
| `/ontogen/result` | `GET .../result/{node\|edge\|triple}` and `.../{id}` (+ `/{id}/attr`, `/{id}/event`) | `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

The **Nodes**, **Edges**, and **Triples** tabs each render their result set
as a compact table — one row per item with the kind's identifying columns
(Node: name; Edge: label · semantics; Triple: `subj --edge--> obj`), a status
badge, the LLM confidence, and the per-row review/evidence controls. Each tab
carries a status filter — **All / Approved-only / Unapproved-only** — applied
client-side over the fetched set; *Approved* is `status === "approved"` and
*Unapproved* is every other status (`llm_pending`, `llm_approved`, `rejected`).
Prev/Next pagination over `GET .../result/{node|edge|triple}` is shown only when
the unfiltered total exceeds the page.

Review proceeds **nodes → edges → triples** per
[API §Ontology Generation](../API.md#ontology-generation-spokeontogen). Review
actions adapt to the row's current status — pending (`llm_pending` /
`llm_approved`) offers **Approve** and **Reject**, an `approved` row offers
**Reject** (revoke), and a `rejected` row offers **Approve** (re-approve) —
each posting `method/review` with the corresponding `verdict`. When a triple's
dependencies are not yet human-`approved` the UI disables the approve action
with an inline hint naming the missing dependency; a dependency at
`llm_approved` does not satisfy the gate. Approval flips status in DataSpoke
storage only and does not write to DataHub.

Each result row carries a "Show evidence" disclosure that, on demand, reads
`GET .../result/{node|edge|triple}/{id}/attr` and renders that row's `evidence`
(the adversarial-debate transcript) as-is.

The **Graph** tab renders an interactive force-directed graph of the ontology —
graph nodes are ontogen nodes (`GET .../result/node`) and links are triples
(`GET .../result/triple`, source/target = the subject/object node, labelled by
its edge). Nodes are colored by status and sized by degree; the view supports
drag, zoom/pan, and hover-highlight of a node's neighbors. A filter selects
**All** or **Approved-only** (approved nodes and the triples among them); there
is no unapproved-only view. The graph is read-only — review actions live in the
Nodes/Edges/Triples tables.

The `/ontogen/conf` page renders the singleton conf with all action controls at
the top-right. When not editing the header shows **Edit** and **Run**; entering
edit mode replaces them with **Save** and **Cancel** and hides Run. **Run**
opens a dialog (`POST .../method/run`) with an optional Markdown one-shot prompt
and a "Dry run" checkbox. **Edit** switches the fields to editable; **Save**
persists via `PUT/PATCH .../attr/conf` and **Cancel** discards. There is no
Delete.

```
┌──────────────────────────────────────────────────────────┐
│  OntoGen [ Nodes | Edges | Triples | Graph ]              │
├──────────────────────────────────────────────────────────┤
│  Nodes (result/node)   filter: [ All ▾ ]                  │
│  ┌──────────────┬───────────┬──────┬────────────────────┐ │
│  │ name         │ status    │ conf │ actions            │ │
│  ├──────────────┼───────────┼──────┼────────────────────┤ │
│  │ BOOK         │ approved  │ 0.96 │ [Reject] (revoke)  │ │
│  │ ORDER_LINE   │ pending   │ 0.71 │ [Approve][Reject]  │ │
│  └──────────────┴───────────┴──────┴────────────────────┘ │
│                                                            │
│  Triples (result/triple)   filter: [ Approved ▾ ]         │
│  ┌────────────────────────────────┬─────────┬──────────┐  │
│  │ ORDER_LINE --references--> BOOK │ pending │ [Approve]│  │
│  │   (blocked: ORDER_LINE node pending)                 │  │
│  └────────────────────────────────┴─────────┴──────────┘  │
└──────────────────────────────────────────────────────────┘
        Browser + review (`/ontogen/result`)
```

Write actions — `Run`, `Approve`, `Reject`, seed create/edit/delete, conf
edits — are rendered only when `role ∈ {Editor, Admin}`. Reader users see
the browser with status badges and no action controls.
