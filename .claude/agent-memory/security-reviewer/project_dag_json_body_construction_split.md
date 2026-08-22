---
name: dag-json-body-construction-split
description: The metrics DAGs build the internal-API JSON body two different ways — json.dumps in the three tier DAGs, hand-spliced Jinja-into-JSON in the on-demand metrics.py — and only one of them is structurally safe
metadata:
  type: project
---

`src/workflows/dags/` posts to `/internal/activities/metrics/run` from four DAGs,
and they do not agree on how the body is built.

- `metrics_{hourly,daily,weekly}.py` — a `@task` returns
  `json.dumps({"metric_id": ..., "scheduled_at": ...})`. Escaping is the JSON
  encoder's job. **Safe by construction.**
- `metrics.py` (on-demand, triggered by `POST /spoke/governance/{metric_id}/method/run`)
  — `data=` is a hand-written JSON *string literal* with Jinja spliced inside the
  double quotes: `'{"metric_id": "{{ dag_run.conf.get(\'metric_id\', \'\') }}", ...}'`.
  Nothing escapes the value; a `"` in `metric_id` would break out of the JSON
  string.

**Why it does not currently break:** `metric_id` is validated at *both* ends by
`_METRIC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$"` — declared
twice, in `src/api/routers/spoke/governance.py:39` and
`src/api/routers/internal/activities.py:282`. Measured: fully anchored (trailing
`$` present, so the usual pydantic-v2 rust-regex "no `$` = prefix match" trap in
[[pydantic-v2-pattern-anchoring]] does not apply here), and it admits no quote,
backslash, newline or control character.

**How to apply:** the splicing is safe *only* because that pattern holds, in two
files that must stay in sync. Treat any diff that widens either copy of
`_METRIC_ID_PATTERN`, or that adds a second Jinja-interpolated field to
`metrics.py`'s `data=`, as a JSON-injection review — not a formatting change. The
right fix direction is to make `metrics.py` use the `json.dumps`-in-a-`@task`
shape its three siblings already use, which removes the dependency entirely.

Related: [[pydantic-v2-pattern-anchoring]], [[metrics-measurement-instant-boundary]]
