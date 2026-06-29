"""Unit tests for src/backend/admin/dag_control_service.py.

The DAG-control service is a pure proxy over Airflow's per-DAG ``is_paused`` flag.
These tests inject a fake AirflowClient so the failure-mode and group-fold contracts
can be exercised without live infra (the spot suite covers the wired happy paths).

Concerns covered:

1. Airflow transport failure → AirflowUnavailableError (503 AIRFLOW_UNAVAILABLE) on
   BOTH read (get_dag_groups) and write (set_group_paused). This is the failure mode
   the live-infra spot tests cannot easily reach (you cannot down Airflow mid-run).

2. Unknown group → EntityNotFoundError(error_code DAG_GROUP_NOT_FOUND, → 404), raised
   BEFORE any Airflow call.

3. set_group_paused issues one set_dag_paused per member DAG of the group (every
   member, no partial loop) — folded against a fresh read.

Spec traceability:
- API.md §Admin (/admin/dags) — when Airflow is unreachable both routes return
  503 AIRFLOW_UNAVAILABLE (API.md L585); unknown group → 404 DAG_GROUP_NOT_FOUND;
  PATCH sets is_paused on every member DAG (API.md L582-584).
- feature/BACKEND.md §Schedule Control — Airflow is SSOT; map of groups → DAGs.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.backend.admin.dag_control_service import (
    GROUP_TO_DAG_IDS,
    get_dag_groups,
    set_group_paused,
)
from src.shared.exceptions import AirflowUnavailableError, EntityNotFoundError


def _airflow(**methods) -> AsyncMock:
    """Build a fake AirflowClient with the two methods the service calls."""
    client = AsyncMock()
    client.get_dag_paused_states = methods.get(
        "get_dag_paused_states", AsyncMock(return_value={})
    )
    client.set_dag_paused = methods.get("set_dag_paused", AsyncMock(return_value=None))
    return client


@pytest.mark.asyncio
async def test_get_dag_groups_airflow_transport_error_maps_to_503() -> None:
    """A read transport failure surfaces as AirflowUnavailableError (→ 503).

    spec: API.md §Admin — when Airflow is unreachable GET /admin/dags returns
        503 AIRFLOW_UNAVAILABLE (API.md L585).
    """
    airflow = _airflow(
        get_dag_paused_states=AsyncMock(side_effect=httpx.ConnectError("down"))
    )

    with pytest.raises(AirflowUnavailableError) as exc_info:
        await get_dag_groups(airflow)

    assert exc_info.value.error_code == "AIRFLOW_UNAVAILABLE"


@pytest.mark.asyncio
async def test_set_group_paused_airflow_transport_error_maps_to_503() -> None:
    """A write transport failure surfaces as AirflowUnavailableError (→ 503).

    spec: API.md §Admin — when Airflow is unreachable PATCH /admin/dags/{group}
        returns 503 AIRFLOW_UNAVAILABLE (API.md L585).
    """
    airflow = _airflow(
        set_dag_paused=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    )

    with pytest.raises(AirflowUnavailableError) as exc_info:
        await set_group_paused(airflow, "ontogen", True)

    assert exc_info.value.error_code == "AIRFLOW_UNAVAILABLE"


@pytest.mark.asyncio
async def test_set_group_paused_unknown_group_raises_404_before_airflow() -> None:
    """An unknown group raises DAG_GROUP_NOT_FOUND (→ 404) without touching Airflow.

    spec: API.md §Admin — an unknown ``group`` returns 404 DAG_GROUP_NOT_FOUND.
    """
    airflow = _airflow()

    with pytest.raises(EntityNotFoundError) as exc_info:
        await set_group_paused(airflow, "not_a_real_group", True)

    assert exc_info.value.error_code == "DAG_GROUP_NOT_FOUND"
    airflow.set_dag_paused.assert_not_awaited()
    airflow.get_dag_paused_states.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_group_paused_sets_every_member_dag() -> None:
    """set_group_paused issues one set_dag_paused per member DAG of the group.

    Pins the 'sets is_paused on EVERY member DAG' contract at the unit level so a
    partial/early-breaking loop is caught independently of live Airflow.

    spec: API.md §Admin — PATCH sets is_paused on every member DAG (API.md L582-584).
    spec: feature/BACKEND.md §Schedule Control — group → DAG map.
    """
    members = GROUP_TO_DAG_IDS["ontogen"]
    # Read-back reports every member paused so the fold returns paused=True.
    airflow = _airflow(
        get_dag_paused_states=AsyncMock(return_value={m: True for m in members})
    )

    status = await set_group_paused(airflow, "ontogen", True)

    # One set_dag_paused per member, each with paused=True — no member skipped.
    called_dag_ids = {call.args[0] for call in airflow.set_dag_paused.call_args_list}
    assert called_dag_ids == set(members)
    assert all(call.args[1] is True for call in airflow.set_dag_paused.call_args_list)
    assert status.group == "ontogen"
    assert status.paused is True
    assert status.mixed is False
    assert {d.dag_id for d in status.dags} == set(members)
