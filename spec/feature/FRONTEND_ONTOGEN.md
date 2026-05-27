# DataSpoke Frontend — Ontology Generation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

OntoGen hosts the singleton operational conf, the Markdown seed library,
the triple-ontology browser, and the approval queue. Approval lives here,
gated by Editor / Admin role.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/ontogen` | Browser + review | `/spoke/ontogen/result/{node\|edge\|triple}/...` |
| `/ontogen/conf` | Conf editor | `/spoke/ontogen/attr/conf` |
| `/ontogen/seed` | Seed library | `/spoke/ontogen/attr/seed/...` |

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/ontogen/conf` | `GET /spoke/ontogen/attr/conf` | `PUT/PATCH/DELETE .../attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) |
| `/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}` |
| `/ontogen` | `GET .../result/{node\|edge\|triple}` (+ `/{id}`, `/attr`, `/event`) | `POST .../method/run` (optional Markdown body — one-shot prompt; `?dry_run=true`); `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

Review proceeds **nodes → edges → triples** per
[API §Ontology Generation](../API.md#ontology-generation-spokeontogen). When
a triple's dependencies are not yet approved the UI disables the approve
button with an inline hint naming the missing dependency. The confirm
dialog states that approval flips status in DataSpoke storage only and does
not write to DataHub.

```
┌──────────────────────────────────────────────────────┐
│  OntoGen          [ Nodes | Edges | Triples ] [Run]  │
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
        Browser + review (`/ontogen`)
```

Write actions — `Run`, `Approve`, `Reject`, seed create/edit/delete, conf
edits — are rendered only when `role ∈ {Editor, Admin}`. Reader users see
the browser with status badges and no action controls.
