# DataSpoke Frontend — Ontology Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

OntoGen hosts the singleton operational conf, the Markdown seed library,
and the triple-ontology browser with inline review. Approval lives here —
inline within each per-type panel and the Navigator — gated by Editor /
Admin role.

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
| `/ontogen/conf` | `GET /spoke/ontogen/attr/conf` | `PUT/PATCH .../attr/conf` (Edit/Save; fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) — Edit and Run controls sit top-right; the conf is a singleton so the UI exposes no Delete. `POST .../method/run` via Run dialog (optional Markdown body — one-shot prompt; "Dry run — evaluate without persisting results" checkbox → `?dry_run=true`) |
| `/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}` |
| `/ontogen/result` | `GET .../result/{node\|edge\|triple}` and `.../{id}` (+ `/{id}/attr`, `/{id}/event`) | `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

Review proceeds **nodes → edges → triples** per
[API §Ontology Generation](../API.md#ontology-generation-spokeontogen). When
a triple's dependencies are not yet human-`approved` the UI disables the
approve button with an inline hint naming the missing dependency; a
dependency at `llm_approved` does not satisfy the gate. Approve and reject
are direct actions — approval flips status in DataSpoke storage only and
does not write to DataHub.

The **Navigator** tab embeds `OntologyNavigator`, overlaying all outgoing
triples for a node (pending + approved) with inline approve/reject controls
on the pending ones.

The `/ontogen/conf` page renders the singleton conf with **Edit** and **Run**
controls at the top-right. **Run** opens a dialog (`POST .../method/run`) with
an optional Markdown one-shot prompt and a "Dry run" checkbox, sitting next to
the conf it runs against. **Edit** switches the fields to editable and saves via
`PUT/PATCH .../attr/conf`; there is no Delete.

```
┌──────────────────────────────────────────────────────┐
│  OntoGen [ Nodes | Edges | Triples | Navigator ]     │
├──────────────────────────────────────────────────────┤
│  Nodes  (result/node)                                │
│    BOOK         conf 0.96   ✓ approved               │
│    ORDER_LINE   conf 0.71   ⏳ pending               │
│       reason: [_______________]  [Approve] [Reject]  │
│                                                      │
│  Triples  (result/triple)                            │
│    ORDER_LINE --references--> BOOK   ⏳              │
│       [Approve] (blocked: ORDER_LINE node pending)   │
│    ORDER_LINE --placed_by --> CUSTOMER ⏳            │
│       [Approve] (blocked: ORDER_LINE node pending)   │
└──────────────────────────────────────────────────────┘
        Browser + review (`/ontogen/result`)
```

Write actions — `Run`, `Approve`, `Reject`, seed create/edit/delete, conf
edits — are rendered only when `role ∈ {Editor, Admin}`. Reader users see
the browser with status badges and no action controls.
