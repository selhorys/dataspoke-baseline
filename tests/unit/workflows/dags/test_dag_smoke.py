"""Smoke tests for all DAG files (except datahub_sync_hourly.py which has its own test).

Tests (parametrized over 14 DAG files):
(a) The file exists and can be read without error.
(b) The DAG file declares a _DAG_ID constant (string literal).
(c) The declared dag_id appears in ALL_DAG_IDS from the registry.
(d) The file declares exactly one dagrun_timeout=timedelta(hours=N), where N matches the
    expected bound: 1 hour by default, with per-DAG exceptions for the ingestion-active
    tier DAGs and auth-role-sync-daily.

Plus one non-parametrized invariant test: auth-role-sync-daily's retry budget
(execution_timeout x (retries + 1) + retry delays) fits inside its dagrun_timeout.

Airflow is not installed in the unit-test environment; tests use Path.read_text()
to inspect the source, following the pattern of test_datahub_sync_daily.py.

spec: feature/BACKEND.md §DAG Catalogue — each DAG file must declare its dag_id
      and that ID must be registered in ALL_DAG_IDS.
spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts — DAG-level dagrun_timeout
      default and per-DAG exceptions.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.workflows.registry import ALL_DAG_IDS

_DAGS_DIR = Path(__file__).resolve().parents[4] / "src" / "workflows" / "dags"

# All .py files except helpers, __init__, and datahub_sync_hourly (has its own test)
_EXCLUDED = frozenset({"_internal_headers.py", "__init__.py", "datahub_sync_hourly.py"})

_DAG_FILES: list[Path] = [
    f for f in sorted(_DAGS_DIR.glob("*.py"))
    if f.name not in _EXCLUDED
]

# Regex to extract _DAG_ID = "..." or _DAG_ID = '...'
_DAG_ID_RE = re.compile(r'_DAG_ID\s*=\s*["\']([^"\']+)["\']')


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_file_exists(dag_file: Path) -> None:
    """DAG file must exist in src/workflows/dags/.

    spec: feature/BACKEND.md §DAG Catalogue — every catalogued DAG has a file.
    """
    assert dag_file.is_file(), f"DAG file not found: {dag_file}"


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_file_declares_dag_id_constant(dag_file: Path) -> None:
    """Each DAG file must declare a _DAG_ID string constant.

    spec: feature/BACKEND.md §DAG Catalogue — dag_id is the stable identifier
          used by Airflow and the registry.
    """
    source = dag_file.read_text(encoding="utf-8")
    match = _DAG_ID_RE.search(source)
    assert match is not None, (
        f"{dag_file.name} must declare '_DAG_ID = \"<dag-id>\"'. "
        "spec: feature/BACKEND.md §DAG Catalogue."
    )


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_id_is_registered_in_all_dag_ids(dag_file: Path) -> None:
    """The _DAG_ID in each DAG file must appear in ALL_DAG_IDS.

    spec: feature/BACKEND.md §DAG Catalogue — registry is the single source of truth.
    """
    source = dag_file.read_text(encoding="utf-8")
    match = _DAG_ID_RE.search(source)
    if match is None:
        pytest.skip(f"{dag_file.name}: no _DAG_ID found (checked separately by file-exists test)")

    dag_id = match.group(1)
    assert dag_id in ALL_DAG_IDS, (
        f"{dag_file.name} declares dag_id='{dag_id}' which is NOT in ALL_DAG_IDS. "
        f"Add it to src/workflows/registry.py or rename the DAG. "
        f"ALL_DAG_IDS: {sorted(ALL_DAG_IDS)}"
    )


# ── Every DAG enforces a bounded run timeout (1h default, per-DAG exceptions) ─

# spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "Per-DAG exceptions to the
# 1-hour `dagrun_timeout`: `ingestion-active-hourly` = 3 hours, `ingestion-active-daily`/
# `ingestion-active-weekly` = 6 hours, ... `auth-role-sync-daily` = 2 hours, to fit its
# 15-minute per-task exception below across a full 4-attempt retry budget."
_DAGRUN_TIMEOUT_HOURS_BY_STEM: dict[str, int] = {
    "ingestion_active_hourly": 3,
    "ingestion_active_daily": 6,
    "ingestion_active_weekly": 6,
    "auth_role_sync_daily": 2,
}
_DEFAULT_DAGRUN_TIMEOUT_HOURS = 1

_DAGRUN_TIMEOUT_RE = re.compile(r"dagrun_timeout\s*=\s*timedelta\(hours\s*=\s*(\d+)\)")


def test_dagrun_timeout_exception_map_keys_are_real_dag_files() -> None:
    """Every key in the expected-dagrun_timeout exception map names a real DAG file.

    Without this check, a DAG rename (or a typo in the map) would make the exception
    silently fall out of the map: the file stem would stop matching, the parametrized
    test below would fall back to the 1-hour default, and the rename could quietly widen
    or shrink the enforced bound with no test failure to flag it.

    spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "Per-DAG exceptions
          to the 1-hour `dagrun_timeout`" list.
    """
    dag_stems = {f.stem for f in _DAG_FILES}
    for stem in _DAGRUN_TIMEOUT_HOURS_BY_STEM:
        assert stem in dag_stems, (
            f"'{stem}' is listed in the dagrun_timeout exception map but no such DAG file "
            f"exists under {_DAGS_DIR}. Known DAG files: {sorted(dag_stems)}"
        )


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_declares_expected_dagrun_timeout(dag_file: Path) -> None:
    """Each DAG declares exactly one dagrun_timeout=timedelta(hours=N) matching its expected bound.

    A DAG with no run timeout hangs unbounded if a task wedges; the bound makes a stuck run
    fail loudly instead. Four DAGs override the 1-hour default (see
    ``_DAGRUN_TIMEOUT_HOURS_BY_STEM``); every other DAG must keep the default. Source-text
    check: Airflow is not installed in the unit environment, so the DAG object cannot be
    constructed — the assertion is that the bound is declared in the file, not that it was
    applied by the scheduler.

    spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "DAG-level = 1 hour by
          default, enforced via `dagrun_timeout` on every DAG's `DAG(...)` constructor call."
          plus the per-DAG exceptions list.
    """
    source = dag_file.read_text(encoding="utf-8")
    matches = _DAGRUN_TIMEOUT_RE.findall(source)
    assert len(matches) == 1, (
        f"{dag_file.name} must declare exactly one dagrun_timeout=timedelta(hours=N) on its "
        f"DAG(...) call, found {len(matches)}: {matches}. "
        "spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts."
    )
    actual_hours = int(matches[0])
    expected_hours = _DAGRUN_TIMEOUT_HOURS_BY_STEM.get(
        dag_file.stem, _DEFAULT_DAGRUN_TIMEOUT_HOURS
    )
    assert actual_hours == expected_hours, (
        f"{dag_file.name} declares dagrun_timeout=timedelta(hours={actual_hours}), but the "
        f"spec expects hours={expected_hours}. "
        "spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts."
    )


# ── auth-role-sync-daily: retry budget must fit inside its dagrun_timeout ────


def _dag_constructor_call(source: str) -> ast.Call:
    """Return the ast.Call node for the ``with DAG(...) as dag:`` constructor invocation."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "DAG"
                ):
                    return call
    raise AssertionError("no 'with DAG(...) as dag:' constructor call found in source")


def _call_keyword(call: ast.Call, name: str) -> ast.expr:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    raise AssertionError(f"DAG(...) call has no '{name}' keyword argument")


def _dict_key(node: ast.expr, name: str) -> ast.expr:
    assert isinstance(node, ast.Dict), f"expected a dict literal, got {ast.dump(node)}"
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == name:
            return value
    raise AssertionError(f"dict literal has no '{name}' key")


def _timedelta_kwarg(node: ast.expr, unit: str) -> int | float:
    """Extract the numeric value of ``unit=`` from a ``timedelta(unit=N)`` call node."""
    assert isinstance(node, ast.Call), f"expected a timedelta(...) call, got {ast.dump(node)}"
    assert isinstance(node.func, ast.Name) and node.func.id == "timedelta", (
        f"expected timedelta(...), got {ast.dump(node.func)}"
    )
    for kw in node.keywords:
        if kw.arg == unit:
            assert isinstance(kw.value, ast.Constant), (
                f"timedelta({unit}=...) must be a literal, got {ast.dump(kw.value)}"
            )
            return kw.value.value
    raise AssertionError(f"timedelta(...) call has no '{unit}' keyword argument")


def test_auth_role_sync_daily_retry_budget_fits_inside_dagrun_timeout() -> None:
    """(retries + 1) x execution_timeout + retries x retry_delay must not exceed dagrun_timeout.

    ``auth-role-sync-daily``'s 2-hour dagrun_timeout is sized to cover a full 4-attempt retry
    budget (1 initial attempt + 3 retries) at its 15-minute per-task execution_timeout, plus
    the retry delays between attempts. If any of these four values drift apart — e.g.
    execution_timeout raised without dagrun_timeout following — a wedged task could exhaust its
    retry budget only for the DAG-level timeout to fire first and kill the run mid-retry.
    AST-parsed rather than regexed because the four values live at different nesting depths
    (two directly on the ``DAG(...)`` call, two inside its nested ``default_args`` dict), which
    makes a flat regex fragile to reformatting.

    spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "`auth-role-sync-daily` =
          2 hours, to fit its 15-minute per-task exception below across a full 4-attempt retry
          budget (execution_timeout × (retries + 1) + retry delays)."
    """
    dag_file = _DAGS_DIR / "auth_role_sync_daily.py"
    source = dag_file.read_text(encoding="utf-8")
    dag_call = _dag_constructor_call(source)

    dagrun_timeout_hours = _timedelta_kwarg(_call_keyword(dag_call, "dagrun_timeout"), "hours")

    default_args = _call_keyword(dag_call, "default_args")
    retries_node = _dict_key(default_args, "retries")
    assert isinstance(retries_node, ast.Constant), (
        f"default_args['retries'] must be a literal, got {ast.dump(retries_node)}"
    )
    retries = retries_node.value
    retry_delay_seconds = _timedelta_kwarg(_dict_key(default_args, "retry_delay"), "seconds")
    execution_timeout_minutes = _timedelta_kwarg(
        _dict_key(default_args, "execution_timeout"), "minutes"
    )

    # Pin the two inputs the spec names explicitly, so a drift in either (e.g. dropping the
    # 15-minute override, or cutting retries) fails here even though it would not, by itself,
    # violate the relative budget-vs-timeout inequality below.
    assert execution_timeout_minutes == 15, (
        f"auth_role_sync_daily.py: default_args['execution_timeout'] = "
        f"timedelta(minutes={execution_timeout_minutes}), expected minutes=15. "
        "spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "
        '"`auth-role-sync-daily` also overrides its per-task `execution_timeout` to 15 minutes".'
    )
    assert retries == 3, (
        f"auth_role_sync_daily.py: default_args['retries'] = {retries}, expected 3 "
        "(1 initial attempt + 3 retries = a full 4-attempt retry budget). "
        "spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts: "
        '"a full 4-attempt retry budget (execution_timeout × (retries + 1) + retry delays)".'
    )

    budget_minutes = (retries + 1) * execution_timeout_minutes + retries * (
        retry_delay_seconds / 60
    )
    dagrun_timeout_minutes = dagrun_timeout_hours * 60

    assert budget_minutes <= dagrun_timeout_minutes, (
        f"auth_role_sync_daily.py: (retries+1)*execution_timeout + retries*retry_delay = "
        f"{budget_minutes} minutes exceeds dagrun_timeout = {dagrun_timeout_minutes} minutes. "
        "spec: feature/BACKEND.md §Workflow Design Conventions — Timeouts."
    )


# ── Metrics tier DAGs forward their scheduled boundary time ──────────────────

_METRICS_TIER_DAGS = ["metrics_hourly.py", "metrics_daily.py", "metrics_weekly.py"]


@pytest.mark.parametrize("dag_name", _METRICS_TIER_DAGS)
def test_metrics_tier_dag_sends_scheduled_at_in_its_run_body(dag_name: str) -> None:
    """Each tier DAG puts a `scheduled_at` field in the run request body it builds.

    Without it, a retried or backlogged tier run measures the interval it *executed* in
    rather than the one it is *for*, and the internal route's `scheduled_at` is dead
    weight. All three tiers are checked because the DAG files are near-duplicates, so a
    fix applied to one is easy to forget on the others.

    Source-text check: Airflow is not importable in the unit environment, so the DAG's
    task body cannot be executed or its Jinja rendered — the assertion is that the field
    is present in the constructed body, not what it renders to.

    spec: feature/BACKEND.md §Metrics Service — Measurement instant: for a "Periodic tier
    DAG (`metrics-{hourly,daily,weekly}`)" the instant is "The DAG run's scheduled
    boundary time (Airflow `data_interval_end`), forwarded as `scheduled_at` on the
    internal run request".
    """
    source = (_DAGS_DIR / dag_name).read_text(encoding="utf-8")
    assert '"scheduled_at": scheduled_at' in source, (
        f"{dag_name} must include scheduled_at in the JSON body it builds for "
        "/internal/activities/metrics/run. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )


@pytest.mark.parametrize("dag_name", _METRICS_TIER_DAGS)
def test_metrics_tier_dag_templates_scheduled_at_from_the_dag_run_interval(
    dag_name: str,
) -> None:
    """The forwarded instant is templated from the DAG run's own interval, not from now().

    `data_interval_end` is the scheduled boundary the spec names. `run_after` is the
    fallback for a manually-triggered run, which has no data interval — an unguarded
    `data_interval_end` would render as `None` there and the route would reject the body.
    Both spellings are pinned so neither half can be dropped.

    A rendered-value assertion is out of reach here (no Airflow, no execution context),
    so what is asserted is that the value is a Jinja expression over the DAG run rather
    than a Python-side clock read: a `datetime.now()` in the DAG body would date the run
    at *parse* time, which is a different instant on every scheduler heartbeat.

    spec: feature/BACKEND.md §Metrics Service — Measurement instant, trigger table:
    "The DAG run's scheduled boundary time (Airflow `data_interval_end`), forwarded as
    `scheduled_at` on the internal run request. A manually-triggered run of one of these
    DAGs carries no `data_interval_end`; the DAG falls back to `dag_run.run_after`
    (always present, ≈ the trigger instant) so a manual trigger still renders rather
    than failing at template time".
    """
    source = (_DAGS_DIR / dag_name).read_text(encoding="utf-8")
    assert "dag_run.data_interval_end" in source, (
        f"{dag_name} must template scheduled_at from dag_run.data_interval_end. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )
    assert "dag_run.run_after" in source, (
        f"{dag_name} must fall back to dag_run.run_after for a manual trigger, which "
        "carries no data interval; an unguarded data_interval_end renders as None there."
    )
    assert ".isoformat() }}" in source, (
        f"{dag_name} must forward an RFC 3339 string — the route parses an "
        "AwareDatetime, and a raw pendulum repr is not one."
    )
    assert "datetime.now" not in source, (
        f"{dag_name} must not read a clock in the DAG body: the instant is the DAG run's "
        "own interval boundary, not the moment the file was parsed. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )


def test_dag_file_count_is_exactly_14() -> None:
    """There must be exactly 14 DAG files under test (excluding datahub_sync_hourly).

    spec: feature/BACKEND.md §DAG Catalogue — 15 total = 14 + datahub_sync_hourly.
    """
    assert len(_DAG_FILES) == 14, (
        f"Expected 14 DAG files under test (15 total - 1 for datahub_sync_hourly), "
        f"found {len(_DAG_FILES)}: {[f.name for f in _DAG_FILES]}"
    )
