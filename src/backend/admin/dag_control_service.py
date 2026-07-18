"""Admin DAG schedule-control service — pause/unpause Airflow DAG groups.

A pure proxy over Airflow's per-DAG ``is_paused`` flag. Airflow is the SSOT for
paused state — DataSpoke stores no copy (no DB column, no runtime-config field).
The group→DAG map below is the single source of truth for which DAGs each
controllable group covers.

A group is reported ``paused`` only when **all** its member DAGs are paused;
``mixed`` is ``true`` when members disagree (some paused, some not).

Spec: spec/feature/BACKEND.md §Schedule Control, spec/API.md (/admin/dags).
"""

import logging

import httpx

from src.api.schemas.admin import DagDetail, DagGroup, DagGroupStatus
from src.shared.exceptions import AirflowUnavailableError, EntityNotFoundError
from src.workflows.airflow.client import AirflowClient

logger = logging.getLogger(__name__)

# The controllable groups and their member DAGs — single source of truth.
GROUP_TO_DAG_IDS: dict[DagGroup, tuple[str, ...]] = {
    "datahub_sync": ("datahub-sync-hourly",),
    "auth_role_sync": ("auth-role-sync-daily",),
    "ingestion_active": (
        "ingestion-active-hourly",
        "ingestion-active-daily",
        "ingestion-active-weekly",
    ),
    "ontogen": ("ontogen-hourly", "ontogen-daily", "ontogen-weekly"),
    "metagen": ("metagen-hourly", "metagen-daily", "metagen-weekly"),
    "metrics": ("metrics-hourly", "metrics-daily", "metrics-weekly"),
}


def _fold_group(group: DagGroup, paused_states: dict[str, bool]) -> DagGroupStatus:
    """Fold per-DAG paused state into a single group status.

    A DAG missing from ``paused_states`` (not yet loaded by Airflow) is treated
    as not paused so a half-loaded estate never reports a group as fully paused.
    """
    dag_ids = GROUP_TO_DAG_IDS[group]
    dags = [
        DagDetail(dag_id=dag_id, paused=paused_states.get(dag_id, False))
        for dag_id in dag_ids
    ]
    all_paused = all(d.paused for d in dags)
    any_paused = any(d.paused for d in dags)
    return DagGroupStatus(
        group=group,
        paused=all_paused,
        mixed=any_paused and not all_paused,
        dags=dags,
    )


async def get_dag_groups(airflow: AirflowClient) -> list[DagGroupStatus]:
    """Return the schedule (paused) status of every controllable group.

    Reads paused state for every member DAG in one Airflow call.
    Raises AirflowUnavailableError (503) on a transport failure.
    """
    try:
        paused_states = await airflow.get_dag_paused_states()
    except httpx.HTTPError as exc:
        logger.warning("airflow_dag_read_failed", exc_info=True)
        raise AirflowUnavailableError(
            "Airflow REST API did not respond while reading DAG paused state"
        ) from exc
    return [_fold_group(group, paused_states) for group in GROUP_TO_DAG_IDS]


async def set_group_paused(
    airflow: AirflowClient, group: str, paused: bool
) -> DagGroupStatus:
    """Set ``is_paused`` on every member DAG of a group, then return its status.

    Raises EntityNotFoundError (404 DAG_GROUP_NOT_FOUND) for an unknown group and
    AirflowUnavailableError (503) on a transport failure.
    """
    if group not in GROUP_TO_DAG_IDS:
        raise EntityNotFoundError("dag_group", group)
    try:
        for dag_id in GROUP_TO_DAG_IDS[group]:
            await airflow.set_dag_paused(dag_id, paused)
        paused_states = await airflow.get_dag_paused_states()
    except httpx.HTTPError as exc:
        logger.warning(
            "airflow_dag_set_failed",
            extra={"group": group, "paused": paused},
            exc_info=True,
        )
        raise AirflowUnavailableError(
            "Airflow REST API did not respond while setting DAG paused state"
        ) from exc
    return _fold_group(group, paused_states)
