# DataSpoke Frontend — Data Governance (DG) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Layout and shared components in [FRONTEND_BASIC](FRONTEND_BASIC.md).
> API routes in [API](API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Navigation](#navigation)
3. [Metrics Dashboard (UC6)](#metrics-dashboard-uc6)
4. [Multi-Perspective Overview (UC8)](#multi-perspective-overview-uc8)

---

## Overview

The DG workspace focuses on **enterprise-wide observability**: health metrics across departments, visual exploration of the data estate, and governance blind spot detection. DG features consume `/api/v1/spoke/dg/` routes (metric, overview) and `/api/v1/spoke/common/` for shared resources.

---

## Navigation

```
┌───────────┐
│  DG       │
│  ───────  │
│  Home     │
│  Metrics  │
│  Overview │
│  Search   │
│  ───────  │
│  [DE][DA] │
└───────────┘
```

| Item | Route | API Base |
|------|-------|----------|
| Home | `/dg` | — |
| Metrics | `/dg/metrics` | `/spoke/dg/metric/` |
| Overview | `/dg/overview` | `/spoke/dg/overview/` |
| Search | `/dg/search` | `/spoke/common/search` |

---

## Metrics Dashboard (UC6)

### Dashboard Home (`/dg/metrics`)

Enterprise-wide health dashboard with department breakdown. Uses `GET /spoke/dg/metric`.

```
┌────────────────────────────────────────────────────────────┐
│  Enterprise Metadata Health                                │
│                                                            │
│  ┌─ Score Card ──────────────────────────────────────┐    │
│  │                                                    │    │
│  │   Enterprise Score         Trend (90 days)         │    │
│  │   ┌─────────┐            ┌───────────────────┐    │    │
│  │   │         │            │  100 ┤             │    │    │
│  │   │   77    │            │   80 ┤    ╱──────  │    │    │
│  │   │  /100   │            │   60 ┤───╱         │    │    │
│  │   │         │            │   40 ┤             │    │    │
│  │   └─────────┘            │      └──┬──┬──┬──  │    │    │
│  │   Target: 70 ✓           │       Jan Feb Mar  │    │    │
│  │                          └───────────────────┘    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Department Breakdown ────────────────────────────┐    │
│  │                                                    │    │
│  │  Engineering      ████████████████░░░░  76  ↑ +3%  │    │
│  │  Data Science     ██████████████░░░░░░  69  → 0%   │    │
│  │  Marketing        ██████████████████░░  71  ↑ +17% │    │
│  │  Finance          ████████████████████  81  ↑ +5%  │    │
│  │  Operations       █████████░░░░░░░░░░░  45  → 0%   │    │
│  │  Publisher Rel.   ████████░░░░░░░░░░░░  44  ↑ +4%  │    │
│  │                                                    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Critical Issues ─────────────────────────────────┐    │
│  │  42 critical │ 78 high │ 118 medium               │    │
│  │                                                    │    │
│  │  1. marketing.campaign_metrics — no owner (38 usr) │    │
│  │  2. ops.daily_summary — no description (22 usr)    │    │
│  │  3. ...                                            │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### Dashboard Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Score visualization | Large number + bar chart per department | CDO needs instant grasp of enterprise state |
| Trend chart | 90-day line chart (Recharts) | Shows improvement trajectory, matches initiative timeline |
| Issue list | Priority-sorted, paginated | Actionable; click → dataset detail |
| Real-time updates | WS `/spoke/dg/metric/stream` | Dashboard stays current without manual refresh |
| Department click | Drill into department detail view | CDO → department lead handoff |

### Metric List (`/dg/metrics/list`)

Browse all defined metrics. Uses `GET /spoke/dg/metric`.

```
┌────────────────────────────────────────────────────────────┐
│  Metrics                                    [+ New Metric] │
│                                                            │
│  [Search...          ]  Theme: [All v]  Status: [All v]    │
├───────────────────────────┬────────┬──────────┬────────────┤
│  Metric                   │ Value  │  Alarm   │  Trend     │
├───────────────────────────┼────────┼──────────┼────────────┤
│  Poorly documented        │  42    │  ● Off   │  ↓ -8/wk   │
│  Unowned high-usage       │  15    │  ▲ On    │  ↓ -3/wk   │
│  Erroneous Bronze layer   │  23    │  ▲ On    │  → stable  │
│  Data downtime (hrs)      │  4.2   │  ● Off   │  ↓ -1.1/wk │
├───────────────────────────┴────────┴──────────┴────────────┤
│  1-20 of 12                                                │
└────────────────────────────────────────────────────────────┘
```

Row click → metric detail.

### Metric Detail (`/dg/metrics/[metric_id]`)

Shows metric definition, timeseries chart, and events.

```
┌────────────────────────────────────────────────────────────┐
│  ← Metrics / Poorly Documented Datasets                    │
│  Theme: Documentation  │  Period: Weekly  │  ● Active      │
│                                                            │
│  [Edit] [Run Now] [Deactivate]                             │
│                                                            │
│  ┌─ Timeseries ──────────────────────────────────────┐    │
│  │  Count                                             │    │
│  │  80 ┤                                              │    │
│  │  60 ┤──╲                                           │    │
│  │  40 ┤    ╲───────╲                                 │    │
│  │  20 ┤              ╲─────                          │    │
│  │   0 ┤                                              │    │
│  │     └──┬──┬──┬──┬──┬──┬──┬──                      │    │
│  │       W1  W2  W3  W4  W5  W6  W7                  │    │
│  │                                                    │    │
│  │  [1W] [1M] [3M] [6M] [1Y]  Range: [from] [to]    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Alarm Configuration ─────────────────────────────┐    │
│  │  Threshold: > 50  │  Notify: data-gov@imazon.com  │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Events ──────────────────────────────────────────┐    │
│  │  2026-03-05 ✓ Measured: 42 (no alarm)              │    │
│  │  2026-02-26 ✓ Measured: 48 (no alarm)              │    │
│  │  2026-02-19 ▲ Measured: 52 (alarm triggered)       │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Edit** → opens config form. `PUT/PATCH /spoke/dg/metric/{id}/attr/conf`
- **Run Now** → `POST /spoke/dg/metric/{id}/method/run`
- **Activate/Deactivate** → `POST /spoke/dg/metric/{id}/method/activate` or `deactivate`
- **Timeseries** → `GET /spoke/dg/metric/{id}/attr/result?from=...&to=...` rendered as Recharts area chart
- **Events** → `GET /spoke/dg/metric/{id}/event`
- **Time range shortcuts** (1W, 1M, etc.) set `from`/`to` query params

### Metric Config Form

Modal for creating or editing a metric definition.

```
┌──────────────────────────────────────────────┐
│  Metric Definition                           │
│                                              │
│  Title:  [Poorly Documented Datasets    ]    │
│  Theme:  [Documentation             v]       │
│  Period: [Weekly                     v]       │
│                                              │
│  Alarm                                       │
│  [x] Enable alarm                            │
│  Threshold: [>] [50]                         │
│  Notify:    [data-gov@imazon.com    ]        │
│                                              │
│  [Cancel]                    [Save]          │
└──────────────────────────────────────────────┘
```

---

## Multi-Perspective Overview (UC8)

### Overview Page (`/dg/overview`)

Two visualization modes: **Taxonomy Graph** and **Medallion Classification**. Uses `GET /spoke/dg/overview` and `GET/PATCH /spoke/dg/overview/attr`.

```
┌────────────────────────────────────────────────────────────┐
│  Data Estate Overview                                      │
│                                                            │
│  [Taxonomy Graph]  [Medallion]           [Config ⚙]       │
│  ───────────────────────────────────────────────────────   │
│                                                            │
│  (active view content below)                               │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Taxonomy Graph View

Interactive force-directed graph rendered with a graph library (e.g., `react-force-graph` or Highcharts network graph).

```
┌────────────────────────────────────────────────────────────┐
│  Taxonomy Graph       700 nodes │ 1,842 edges              │
│                                                            │
│  Filters: Domain [All v]  Score [All v]  Usage [All v]     │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │                                                    │   │
│  │         ┌──────┐                                   │   │
│  │    ┌──●─┤Catlog├──●──┐                             │   │
│  │    │    └──────┘     │                             │   │
│  │  ┌─┴────┐       ┌───┴───┐     ┌────────┐         │   │
│  │  │Orders│───●───│Review │─────│Recomm. │         │   │
│  │  └──────┘       └───────┘     │ (ALL   │         │   │
│  │    │                          │  RED!) │         │   │
│  │  ┌─┴──────┐                   └────────┘         │   │
│  │  │Shipping│                                       │   │
│  │  └────────┘                                       │   │
│  │                                                    │   │
│  │  ● = dataset node                                  │   │
│  │  Node color: 🔴 <50  🟡 50-70  🟢 >70             │   │
│  │  Node size:  usage volume                          │   │
│  │  Solid edge: lineage │ Dashed: semantic            │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌─ Selected: recommendations.* cluster ─────────────┐    │
│  │  12 datasets │ Avg score: 25 │ No ownership        │    │
│  │  Risk: CRITICAL — undocumented, unowned, high use  │    │
│  │  [View Details] [Export]                            │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### Graph Interaction

| Action | Behavior |
|--------|----------|
| Zoom | Mouse wheel / pinch — zoom in/out |
| Pan | Click + drag on canvas |
| Click node | Select dataset — show detail panel below |
| Click cluster | Select all datasets in ontology category |
| Hover node | Tooltip: name, score, owner, top connections |
| Hover edge | Tooltip: relationship type, direction |
| Filters | Domain, score range, usage range — re-render graph |
| Search | Highlight matching nodes, dim others |

### Graph Config

Accessible via `[Config ⚙]`. Persists via `PATCH /spoke/dg/overview/attr`.

| Setting | Options |
|---------|---------|
| Layout algorithm | Force-directed (default), hierarchical, circular |
| Color by | Health score (default), domain, medallion layer, owner |
| Size by | Usage volume (default), downstream count, column count |
| Edge visibility | Lineage only, semantic only, both (default) |
| Min score filter | Slider 0–100 |

### Medallion Classification View

Tabular + visual view of Bronze/Silver/Gold layer distribution.

```
┌────────────────────────────────────────────────────────────┐
│  Medallion Classification                                  │
│                                                            │
│  ┌─ Layer Summary ───────────────────────────────────┐    │
│  │                                                    │    │
│  │  🥉 Bronze   ██████████████████░░░░░░░░  180       │    │
│  │  🥈 Silver   ████████████░░░░░░░░░░░░░░  120       │    │
│  │  🥇 Gold     █████░░░░░░░░░░░░░░░░░░░░░   55       │    │
│  │  ❓ Unknown  ██████████████████████████░  345       │    │
│  │                                                    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Conversion Funnel ───────────────────────────────┐    │
│  │                                                    │    │
│  │  Bronze(180) ──60%──► Silver(120) ──46%──► Gold(55)│    │
│  │                                                    │    │
│  │  40% of Bronze (72) have NO Silver counterpart     │    │
│  │  → Candidates for cleanup                          │    │
│  │                                                    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Cleanup Candidates ──────────────────────────────┐    │
│  │  publishers.feed_raw_legacy  │ 8mo stale │ 0 dep  │    │
│  │  shipping.carrier_raw_v1     │ 6mo stale │ 0 dep  │    │
│  │  marketing.campaign_2022     │ 11mo stale│ 0 dep  │    │
│  │  ...                                               │    │
│  │  Estimated recoverable storage: ~2.3 TB            │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Unclassified Triage ─────────────────────────────┐    │
│  │  345 datasets need review                          │    │
│  │  Recommendation: Run Deep Ingestion on top 50      │    │
│  │  → Estimated auto-classify: 180 of 345 (52%)      │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### Medallion Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Layer detection | Auto-classified by backend (lineage depth + naming + schema) | Manual tagging doesn't scale to 700+ datasets |
| Conversion funnel | Visual flow diagram | CDO wants to see data refinement pipeline health |
| Cleanup candidates | Sorted by staleness × zero-dependency | Highest-impact cleanup targets first |
| Unknown triage | Link to Deep Ingestion (UC1) | Cross-feature integration drives adoption |
