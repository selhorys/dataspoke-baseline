"""Ingestion workflow — periodic flow generation and sync logic.

Manual runs call IngestionService.run() directly via the API route.
Periodic ingestion is handled by dynamically generated Kestra flows,
one per unique cron schedule, managed by the ingestion-config-sync flow.
"""

from __future__ import annotations

import hashlib
import logging
import string

logger = logging.getLogger(__name__)

FLOW_ID = "ingestion"
PERIODIC_FLOW_PREFIX = "ingestion-periodic-"

# Kestra YAML template for periodic ingestion flows.
# Uses $-style placeholders (string.Template) to avoid conflicts with
# Kestra's {{ }} expression syntax, which must pass through verbatim.
_PERIODIC_FLOW_TEMPLATE = string.Template(
    """\
id: $flow_id
namespace: dataspoke
description: "Periodic ingestion for schedule: $schedule"

inputs:
  - id: callback_base_url
    type: STRING
    defaults: "$callback_base_url"

triggers:
  - id: cron
    type: io.kestra.plugin.core.trigger.Schedule
    cron: "$schedule"

tasks:
  - id: list_datasets
    type: io.kestra.plugin.core.http.Request
    uri: "{{ inputs.callback_base_url }}/internal/activities/list-periodic-datasets"
    method: POST
    contentType: application/json
    body: |
      {"schedule": "$schedule"}
    retry:
      type: constant
      maxAttempt: 3
      interval: PT10S

  - id: run_each
    type: io.kestra.plugin.core.flow.EachSequential
    value: "{{ outputs.list_datasets.body }}"
    tasks:
      - id: run_ingestion
        type: io.kestra.plugin.core.http.Request
        uri: "{{ inputs.callback_base_url }}/api/v1/spoke/common/data/{{ taskrun.value }}/attr/ingestion/method/run"
        method: POST
        contentType: application/json
        body: |
          {"dry_run": false}
        retry:
          type: constant
          maxAttempt: 3
          interval: PT10S
"""
)


def schedule_to_flow_id(schedule: str) -> str:
    """Return the Kestra flow ID for a given cron schedule string.

    Uses the first 8 hex chars of the MD5 hash of the schedule for a
    stable, human-readable short identifier.
    """
    digest = hashlib.md5(schedule.encode()).hexdigest()[:8]  # noqa: S324
    return f"{PERIODIC_FLOW_PREFIX}{digest}"


def generate_periodic_flow_yaml(schedule: str, callback_base_url: str) -> str:
    """Generate the YAML source for a periodic ingestion flow.

    The returned YAML contains Kestra ``{{ expression }}`` placeholders that
    Kestra evaluates at runtime — these are *not* Python format expressions.
    """
    return _PERIODIC_FLOW_TEMPLATE.substitute(
        flow_id=schedule_to_flow_id(schedule),
        schedule=schedule,
        callback_base_url=callback_base_url,
    )


async def sync_periodic_ingestion_flows(
    kestra_client,
    db,
    callback_base_url: str,
) -> dict:
    """Sync periodic ingestion flows in Kestra based on current configs.

    Steps:
    1. Query distinct schedules from ingestion_configs where periodic=true.
    2. Generate one flow per unique schedule and register it via
       KestraClient.create_or_update_flow().
    3. Delete any ingestion-periodic-* flows whose schedule is no longer
       represented in the configs.

    Returns a dict with keys ``created``, ``deleted``, and ``unchanged``.
    """
    from sqlalchemy import distinct, select

    from src.shared.db.models import IngestionConfig

    # 1. Collect active schedules
    result = await db.execute(
        select(distinct(IngestionConfig.schedule)).where(
            IngestionConfig.periodic == True,  # noqa: E712
            IngestionConfig.schedule.isnot(None),
        )
    )
    active_schedules: set[str] = {row[0] for row in result.all()}
    expected_flow_ids: set[str] = {schedule_to_flow_id(s) for s in active_schedules}

    # 2. Register a flow for every active schedule
    created: list[str] = []
    for schedule in active_schedules:
        flow_id = schedule_to_flow_id(schedule)
        flow_yaml = generate_periodic_flow_yaml(schedule, callback_base_url)
        try:
            await kestra_client.create_or_update_flow(flow_yaml)
            created.append(flow_id)
            logger.info("Registered periodic ingestion flow %s", flow_id)
        except Exception:
            logger.error(
                "Failed to register periodic ingestion flow %s", flow_id, exc_info=True
            )

    # 3. Delete stale ingestion-periodic-* flows
    deleted: list[str] = []
    try:
        existing_flows = await kestra_client.list_flows(prefix=PERIODIC_FLOW_PREFIX)
        existing_flow_ids: set[str] = {f["id"] for f in existing_flows}
        stale_flow_ids = existing_flow_ids - expected_flow_ids
        for flow_id in stale_flow_ids:
            try:
                await kestra_client.delete_flow(flow_id)
                deleted.append(flow_id)
                logger.info("Deleted stale periodic ingestion flow %s", flow_id)
            except Exception:
                logger.error(
                    "Failed to delete stale flow %s", flow_id, exc_info=True
                )
    except Exception:
        logger.error("Failed to list existing periodic flows", exc_info=True)

    unchanged: list[str] = [
        fid for fid in expected_flow_ids if fid not in created
    ]

    return {"created": created, "deleted": deleted, "unchanged": unchanged}
