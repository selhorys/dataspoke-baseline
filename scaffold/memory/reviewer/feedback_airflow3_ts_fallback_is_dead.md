---
name: airflow3-ts-fallback-is-dead
description: An Airflow 3 DAG template `{{ data_interval_end ... else ts }}` fallback never fires — ts and data_interval_end are both absent together, and StrictUndefined turns the manual-trigger path into a task failure
metadata:
  type: feedback
---

In Airflow 3, `logical_date`, `ts`/`ds`/`ts_nodash` **and** `data_interval_{start,end}` are added
to the template context inside **one** `if logical_date := ...:` block
(`airflow/sdk/execution_time/task_runner.py`, `RuntimeTaskInstance.get_template_context` — the
source even comments *"logical_date and data_interval either coexist or be None together"*). A
manual run with no logical date (the Airflow 3 default for UI/API triggers) therefore has
**neither** name in the context, and `DAG.template_undefined` defaults to `jinja2.StrictUndefined`.

So `"{{ data_interval_end.isoformat() if data_interval_end else ts }}"` does not degrade
gracefully — `if data_interval_end` calls `StrictUndefined.__bool__` and raises
`UndefinedError: 'data_interval_end' is undefined`, failing the task (then its retries, then the
DAG run). The `else` arm is unreachable in both directions: when it would be needed, `ts` is
undefined too.

**Why:** review-2 of the metrics `!=`/`NOT IN` + validation-score stage. Three tier DAGs had this
exact fallback added as a "manual trigger safety" fix; it made manual triggering strictly worse
than the untemplated code it replaced. Confirmed by reading the cached task-SDK source and
re-running the render under `StrictUndefined` — not by reasoning alone.

**How to apply:** treat *any* `{{ <interval-or-ts name> ... }}` in a DAG as scheduled-run-only.
`dag_run` is the one always-present handle (set unconditionally in `context_from_server`) and its
`run_after` is non-optional, so
`{{ (dag_run.data_interval_end or dag_run.run_after).isoformat() }}` covers both paths.
Verification recipe without installing Airflow: the uv cache has real sources —
`find ~/.cache/uv/archive-v0 -maxdepth 2 -name "apache_airflow*dist-info"`, then read
`airflow/sdk/execution_time/task_runner.py` and `airflow/sdk/definitions/dag.py`; jinja2 and
markupsafe are cached there too and can be put on `PYTHONPATH` to render the template for real.
Relates to [[verify-branch-reachability-rationales]] — an "else" arm nobody can reach.
