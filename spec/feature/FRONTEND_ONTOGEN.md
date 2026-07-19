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
| `/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}`, `PATCH .../attr/seed/{seed_id}/attr/enabled` (JSON `{is_enabled}`) |
| `/ontogen/result` | `GET .../result/{node\|edge\|triple}` (each row carries `run_id`) | `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

The **Seed library** (`/ontogen/seed`) lists **all** seeds — enabled and disabled — each
row showing an enabled/disabled indicator and a per-seed enable/disable toggle
(`PATCH .../attr/seed/{seed_id}/attr/enabled`). Only enabled seeds steer inference, so a
disabled seed stays in the library and can be re-enabled at any time. `+ New Seed`
(`POST .../attr/seed`) creates the seed **disabled** — the steward enables it once the
body is reviewed. `Delete` is a hard delete behind a [ConfirmDialog](FRONTEND_BASIC.md#shared-component-notes).

The **Nodes**, **Edges**, and **Triples** tabs each render their result set in a
**uniform compact seven-column table** — the same column layout for all three
kinds so the panels read and scale identically:

| Column | Node | Edge | Triple |
|---|---|---|---|
| **Title** | `name` | `label` | `subj --edge--> obj` (monospace) |
| **Description** | `description` | `semantics` (or `—`) | `—` (triples carry no description) |
| **Status** | status badge | status badge | status badge |
| **Confidence** | LLM `score` | same | same |
| **Actions** | Approve / Reject | same | same (dependency-gated) |
| **Created At** | `created_at` (timezone-aware) | same | same |
| **Evidence** | Langfuse **Link** (from `run_id`) | same | same |

The **Description** cell is truncated to a max width with the full text on
hover. **Created At** renders in the global Settings timezone (Local or UTC).

Each tab header carries a **status filter** — **All / Approved / Unapproved**,
applied client-side over the fetched set (*Approved* is `status === "approved"`;
*Unapproved* is every other status — `llm_pending`, `llm_approved`, `rejected`)
— and beside it a **sort control** offering **Created (newest)** and **Created
(oldest)**, which set the `GET .../result/{node|edge|triple}` request's
`?sort=created_at_desc` / `?sort=created_at_asc` (default newest-first) per the
[API sort convention](../API.md#query-parameters). The table foot carries the
shared **[Pagination](FRONTEND_BASIC.md#shared-component-notes)** control
(page-size 20 / 50 / 100, default 20; Prev/Next; numbered pages) over the same
endpoint. Changing the sort, filter, or page size resets `offset` to `0`.

Review proceeds **nodes → edges → triples** per
[API §Ontology Generation](../API.md#ontology-generation-spokeontogen). Review
actions adapt to the row's current status — pending (`llm_pending` /
`llm_approved`) offers **Approve** and **Reject**, an `approved` row offers
**Reject** (revoke), and a `rejected` row offers **Approve** (re-approve).
Choosing an action opens a **review confirmation dialog** carrying a free-text
**reason** field; confirming posts `method/review` with the corresponding
`{verdict, reason}`. When a triple's dependencies are not yet human-`approved`
the UI disables that triple's Approve action and surfaces the missing dependency
as **hover text** on the disabled control; a dependency at `llm_approved` does
not satisfy the gate. Approval flips status in DataSpoke storage only and does
not write to DataHub.

The **Evidence** cell renders a **Link** that opens the row's Langfuse session
in a new tab (`target="_blank" rel="noopener noreferrer"`). The URL is built
client-side as `{langfuse_url}/project/{langfuse_project_id}/sessions/{run_id}`.
Both the host and the project slug resolve by the shared peripheral rule — runtime
config first, then `GET /spoke/common/peripheral-links`
([FRONTEND_BASIC §Shell](FRONTEND_BASIC.md#shell)) — and both denote the
browser-reachable host, not the in-cluster one; `run_id` is the row's creating
inference run.
The session holds every producer/reviewer LLM call of that run — the
adversarial-debate transcript — traced under `session_id = run_id`. The Link
renders only when all three values are present; otherwise the cell shows `—`
(seeded rows have no `run_id`; a deployment with tracing disabled has no
Langfuse config).

The **Graph** tab renders an interactive force-directed graph of the ontology —
graph nodes are ontogen nodes (`GET .../result/node`) and links are triples
(`GET .../result/triple`, source/target = the subject/object node, labelled by
its edge). Nodes are colored by status and sized by degree; the view supports
drag, zoom/pan, and hover-highlight of a node's neighbors. A filter selects
**All** or **Approved-only** (approved nodes and the triples among them); there
is no unapproved-only view. The graph is read-only — review actions live in the
Nodes/Edges/Triples tables.

The `/ontogen/conf` page renders the singleton conf with all action controls at
the top-right. When not editing it shows a read-only **view** of the conf fields
(`is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) as plain
text — the `schedule_tier` value links to its backing Airflow DAG
(`ontogen-<tier>`), and `default_run_prompt` renders as a preformatted text block
(em dash when empty). The header shows **Edit** and **Run**; entering
edit mode replaces them with **Save** and **Cancel** and hides Run. **Run**
opens a dialog (`POST .../method/run`) with an optional Markdown one-shot prompt
and a "Dry run" checkbox. **Edit** swaps the view for the editable form; **Save**
persists via `PUT/PATCH .../attr/conf` and **Cancel** discards. There is no
Delete.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  OntoGen [ Nodes | Edges | Triples | Graph ]                              │
├─────────────────────────────────────────────────────────────────────────┤
│  Nodes (result/node)   filter: [ All ▾ ]   sort: [ Created (newest) ▾ ]   │
│  ┌──────────┬────────────┬────────┬──────────┬─────────┬───────┬────────┐ │
│  │ Title    │ Description │ Status │ Confidence│ Actions │ Created│ Evidence│
│  ├──────────┼────────────┼────────┼──────────┼─────────┼───────┼────────┤ │
│  │ BOOK     │ catalog itm│approved│ 0.96     │ [Reject]│ 2026-…│ [Link] │ │
│  │ ORDER_LN │ line item  │pending │ 0.71     │[Apv][Rj]│ 2026-…│   —    │ │
│  └──────────┴────────────┴────────┴──────────┴─────────┴───────┴────────┘ │
│  size [ 20 ▾ ]   1–2 of 2          [ ‹ Prev ]  1  [ Next › ]              │
│                                                                           │
│  [Link] → Langfuse session in new tab; — when no run_id / tracing off     │
│  [Apv]/[Rj] → confirm dialog w/ reason                                    │
│  disabled [Apv] on a gated triple → hover: "blocked: ORDER_LINE pending"  │
└─────────────────────────────────────────────────────────────────────────┘
        Browser + review (`/ontogen/result`)
```

Write actions — `Run`, `Approve`, `Reject`, seed create/edit/delete, conf
edits — are rendered only when `role ∈ {Editor, Admin}`. Reader users see
the browser with status badges and no action controls.
