---
name: metric-cutoff-vs-measured-at
description: A metric run's window cutoff comes from the measurer's own datetime.now at entry, not from the persisted measured_at (read later in service.py) — any prose/test anchored to "measured_at - time_window_sec" is off by the measurement duration
metadata:
  type: project
---

`ingestion_freshness.measure` / `validation_score.measure` each read
`now = datetime.now(tz=UTC)` at **function entry** and derive `cutoff = now - timedelta(...)`.
`MetricsService.run_metric` reads a **second, later** clock into `measured_at`
(`src/backend/metrics/service.py`, after `_measure` returns) and that is what lands on
`metric_results` / `metric_dataset_results` and feeds `last_check_at`.

**Why:** the #163/#165 spec pass wrote the new inclusive-boundary clause as "evidence whose
instant is exactly `measured_at - time_window_sec`". No code derives that instant. The gap is
the whole measurement duration — scope resolution, `reverse_lookup_batch`, two aggregate
queries — so on a large estate a client reconstructing the window from the stored `measured_at`
plus `detail.time_window_sec` places the boundary later than the measurer did, and evidence in
between reads in-window to the server and out-of-window to the client. It also undercuts the
clause's own "measure-zero" argument, which is about `measured_at`'s resolution.

**How to apply:** reject any spec sentence, docstring, or test docstring that anchors the
window to `measured_at`; the correct phrasing is "the measurement instant — the measurer's
clock reading, taken once per run". Boundary unit tests freeze the measurer's `now`
(`_freeze_now`), so they stay green against the wrong prose — the drift is only catchable by
reading. Related: [[code-option-fix-strands-spec]], [[grep-old-rule-prose-in-consumers]].
