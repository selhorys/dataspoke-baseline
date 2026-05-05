# DataSpoke Frontend — Data Analysis (DA) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared layer in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

DA is a discovery-first surface for analysts. The `/spoke/da/` API tier is
reserved for organization-specific extensions and ships no baseline routes;
DA pages live under `/da/...` and consume baseline features at
`/spoke/common/...` with an analyst-friendly framing — same data sources as
[FRONTEND_DE](FRONTEND_DE.md), different labels.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/da/ontogen` | Ontology browser | `/spoke/common/ontogen/result/...` |
| `/da/validation` | Validation list (fitness framing) | `/spoke/common/validation`, `/spoke/common/data/{urn}/attr/validation/...` |
| `/da/dataset/[urn]` | Dataset detail | `/spoke/common/data/{urn}` |

---

## Page contracts

### Ontogen browser — read-only (UC3)

Three-tab browser over the triple ontology (Nodes / Edges / Triples) backed
by the [FRONTEND_BASIC §Shared Component Notes](FRONTEND_BASIC.md#shared-component-notes)
`OntologyNavigator`.

| Tab | API |
|---|---|
| Nodes | `GET /spoke/common/ontogen/result/node`, `GET .../result/node/{id}/{,/attr,/event}` |
| Edges | `GET /spoke/common/ontogen/result/edge`, `GET .../result/edge/{id}/{,/attr,/event}` |
| Triples | `GET /spoke/common/ontogen/result/triple`, `GET .../result/triple/{id}/{,/attr,/event}` (outgoing-on-node filtered client-side by `subject_node_id`) |

Approval (`POST .../method/review`) is not exposed here — see
[FRONTEND_DG §Ontogen Review](FRONTEND_DG.md#ontogen-review-uc3).

```
┌──────────────────────────────────────────────────────┐
│  Ontology   [ Nodes | Edges | Triples ]              │
├──────────────────────────────────────────────────────┤
│  Nodes  (result/node)                                │
│    BOOK         conf 0.96   member: catalog.books    │
│    CUSTOMER     conf 0.94   member: customers.profile│
│    ORDER_LINE   conf 0.71   member: orders.line_items│
│                                                      │
│  Triples (subject — predicate — object)              │
│    ORDER_LINE --references--> BOOK       conf 0.95   │
│    ORDER_LINE --placed_by --> CUSTOMER   conf 0.87   │
└──────────────────────────────────────────────────────┘
       Browser (`/da/ontogen`) — read-only
```

### Validation — fitness framing (UC2)

Same data sources as DE: `GET /spoke/common/validation` (list) and
`GET /spoke/common/data/{urn}/attr/validation/{conf,result}` plus
`/event/validation`. Run via `POST .../method/validation/run`. No new fields
or endpoints — only the score-label and primary-action wording differ
("Fitness Score" / "Check Fitness" instead of "Quality Score" / "Run
Validation"). The score is the server-provided `quality_score` field on
the same DE-shared endpoints (computed and cached server-side; see
[FRONTEND_DE §Validation](FRONTEND_DE.md#validation-uc2)); rule-type
vocabulary and source-mode vocabulary (for `freshness` / `volume`) are
fixed.

### Dataset detail (`/da/dataset/[urn]`)

Tabs: `Overview / Fitness Check / Ontology`.

| Tab | API |
|---|---|
| Overview | `GET /spoke/common/data/{urn}`, `GET .../attr` |
| Fitness Check | reuses `/da/validation/[urn]` page contract |
| Ontology | `GET /spoke/common/ontogen/result/node` filtered client-side to nodes whose member-dataset list contains this URN, plus `GET .../result/triple` filtered to triples where the matched node is subject or object |

```
┌──────────────────────────────────────────────────────┐
│  ← orders.line_items                                 │
│  [ Overview | Fitness Check | Ontology ]             │
├──────────────────────────────────────────────────────┤
│  (active tab content)                                │
└──────────────────────────────────────────────────────┘
        Aggregator (`/da/dataset/[urn]`)
```
