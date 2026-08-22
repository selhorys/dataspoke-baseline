---
name: metrics-measurement-instant-boundary
description: The governance metrics window arithmetic — where every numeric input that reaches timedelta/datetime is bounded, the measured worst-case headroom, and the jiter 4300-digit ceiling that caps any unbounded int field project-wide
metadata:
  type: project
---

The `validation-score` measurer anchors its window per dataset:

    upper_bound = now - timedelta(seconds=cadence_offset * cadence_unit)
    lower_bound = upper_bound - timedelta(seconds=time_window_sec)

Three inputs feed that arithmetic. All three are now bounded at their write
boundary, and **every bound is the same constant** `MAX_TIME_WINDOW_SEC =
315_360_000` (10y) from `src/shared/metric_conf.py`:

| Input | Written by | Bound | Enforced in |
|---|---|---|---|
| `metric_conf.time_window_sec` | `metric_conf` write boundary | `[1, MAX]` | `src/shared/metric_conf.py` |
| `attribute.cadence_unit` | `PUT/PATCH .../attr/validation/conf` (`require_writer`) | `gt=0, le=MAX` | `ValidationAttribute` field |
| `attribute.cadence_offset` | same | `ge=0`, **no field ceiling** | — |
| `cadence_offset * cadence_unit` | same | `<= MAX` | `ValidationAttribute` `@model_validator(mode="after")` |
| `scheduled_at` | `POST /internal/activities/metrics/run` (`require_internal_token`) | `[now-MAX, now+1d]` | `MetricsRunRequest` `@field_validator` |

**The product check is the load-bearing one.** `cadence_offset` carries no field
ceiling, so bounding `cadence_unit` alone does not bound the shift — only the
model-level product check does. Any future field pair whose *product* reaches
`timedelta` needs the same shape.

**Measured worst-case headroom** (run against the repo's own Python, not
estimated): the widest schema-accepted combination is
`scheduled_at = now - 10y`, shift `= 10y`, width `= 10y` → `lower_bound ≈ 1996`.
That is ~2000 years above `datetime.min` and ~8000 below `datetime.max`. No
schema-accepted tuple overflows. Fuzzing `scheduled_at` with epoch ints ±10^18,
inf/NaN, year-99999 strings, `+99:00` offsets and 100k-char fractions produced
only `ValidationError` — no `OverflowError` escaping as a bare 500.

**The jiter 4300-digit ceiling — reusable beyond this feature.** Pydantic v2's
Rust JSON parser honours `sys.get_int_max_str_digits()` (default 4300) and
rejects any longer integer *literal* with `json_invalid` before an `int` object
exists. Measured by bisection. So an unbounded `int` field cannot be handed a
million-digit bigint: the largest value reaching Python arithmetic has ~4300
digits, bigint multiplication on it is microseconds, and the 422 body stays
~1x the request size. **Do not raise a bigint-CPU-DoS finding on an unbounded
`int` field without re-measuring this** — but note it does *not* defend the
`datetime` range, which is what the bounds above are for.

**Bool coercion is a real hole and is closed here.** `bool` subclasses `int`, so
Pydantic lax mode admits `true` on a plain `int` field as `1`. Both cadence
fields carry a `mode="before"` `_reject_bool`. (`AwareDatetime` rejects bools
natively — `datetime_type` — so `scheduled_at` needs no such guard.)

**The DB is deliberately not a backstop.** Migration and ORM model both carry a
comment saying bounds are "enforced at the API schema layer, so there is
deliberately no CHECK constraint here." `_window_bounds`'s docstring now states
this honestly: defaults fill an *absent* key only, and a row written outside the
API fails the run rather than being clamped. `attribute` is a *new* column in the
squashed `001`, so no legacy row can carry an out-of-range value. Only
`ValidationService.upsert_config` / `patch_config` write it, both fed from
validated `ValidationAttribute` dumps.

**Read-boundary re-validation exists here, unusually:** `ValidationConfResponse.attribute`
is typed `ValidationAttribute`, so a bad stored row 500s on *response* validation
rather than leaking. Fails closed — contrast [[consumer-db-plane-to-wire-boundary]],
where the DB→wire re-validation is missing.

**Residual (low, accepted):** an internal-token holder can date a run up to 10
years back, writing a stale-window result as the metric's latest non-dry run.
Data-integrity only, on a plane that can already trigger arbitrary runs, and
`measured_at` is a separate wall-clock reading so the *result row* is not
backdated. `/internal` is publicly routable per [[internal-surface-exposure-model]].

Related: [[dataset-filter-compile-path-invariant]], [[metric-conf-write-boundary]],
[[api-422-echoes-rejected-input]], [[dag-json-body-construction-split]]
