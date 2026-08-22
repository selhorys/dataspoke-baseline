---
name: unbounded-int-reaches-timedelta
description: A pydantic int field with only a gt/ge bound that later feeds timedelta()/datetime arithmetic is an accepted-value 500 — the API write boundary admits values the measurer cannot compute
metadata:
  type: feedback
---

When a request schema bounds an int on one side only (`gt=0`, `ge=0`) and that value later reaches
`timedelta(seconds=...)` or `now - timedelta(...)`, the write boundary accepts inputs that make the
consumer raise `OverflowError` — not a `DataSpokeError`, so it escapes every handler as a bare 500.

**Why:** `validation_configs.attribute.{cadence_unit, cadence_offset}` (VALIDATION.md gives only
`> 0` / `>= 0`) multiply into `upper_bound = now - timedelta(seconds=offset*unit)` in
`validation_score.measure`. `PUT .../attr/validation/conf {"attribute": {"cadence_unit":
86400000000000, "cadence_offset": 1}}` — a plausible seconds/nanoseconds mix-up — is accepted, and
from then on **every** `validation-score` run whose `dataset_filter` scopes that one dataset 500s
(retries 3x, then the tier DAG task fails). Contrast the sibling knob: `time_window_sec` carries an
explicit `MAX_TIME_WINDOW_SEC` ceiling *and* BACKEND.md documents "an out-of-range stored window
makes every run fail" only for rows written **outside** the API. Here the API is the source.

**How to apply:** for any new numeric field, ask what arithmetic consumes it and probe the
extremes at the schema layer (`10**18`, and the much lower `~63,000 years` datetime-underflow
threshold — `now - timedelta(seconds=10**11)` already raises "date value out of range"). Two more
checks on the same fields: pydantic lax mode coerces `true` → `1` for `int` (so `{"cadence_unit":
true}` silently means 1 second, while `metric_conf.time_window_sec` explicitly rejects booleans),
and a fix needs the ceiling in the spec table too — generators cannot edit `spec/`, see
[[code-option-fix-strands-spec]].
