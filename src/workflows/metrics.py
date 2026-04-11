"""Metrics workflow — periodic flow generation and sync logic.

Manual runs trigger the `metrics` Kestra flow via KestraClient.
Periodic metric execution is handled by dynamically generated Kestra flows,
one per unique cron schedule, managed by the metrics-config-sync flow.
"""

from __future__ import annotations

import hashlib
import logging
import string

logger = logging.getLogger(__name__)

FLOW_ID = "metrics"
PERIODIC_FLOW_PREFIX = "metrics-periodic-"

# Kestra YAML template for periodic metrics flows.
# Uses $-style placeholders (string.Template) to avoid conflicts with
# Kestra's {{ }} expression syntax, which must pass through verbatim.
_PERIODIC_FLOW_TEMPLATE = string.Template(
    """\
id: $flow_id
namespace: dataspoke
description: "Periodic metrics execution for schedule: $schedule"

concurrency:
  limit: 2
  behavior: QUEUE

inputs:
  - id: callback_base_url
    type: STRING
    defaults: "$callback_base_url"

triggers:
  - id: cron
    type: io.kestra.plugin.core.trigger.Schedule
    cron: "$schedule"
    disabled: true
    recoverMissedSchedules: NONE

tasks:
  - id: list_metrics
    type: io.kestra.plugin.core.http.Request
    uri: "{{ inputs.callback_base_url }}/internal/activities/metrics/list-periodic"
    method: POST
    contentType: application/json
    body: |
      {"schedule_cron": "$schedule"}
    options:
      connectTimeout: PT5S
      readTimeout: PT30S
    retry:
      type: constant
      maxAttempt: 3
      interval: PT10S

  - id: run_each
    type: io.kestra.plugin.core.flow.EachParallel
    value: "{{ outputs.list_metrics.body }}"
    concurrent: $concurrent
    tasks:
      - id: run_metric
        type: io.kestra.plugin.core.http.Request
        uri: "{{ inputs.callback_base_url }}/internal/activities/metrics/run"
        method: POST
        contentType: application/json
        body: |
          {"metric_id": "{{ taskrun.value }}", "dry_run": false}
        options:
          connectTimeout: PT5S
          readTimeout: PT30S
        retry:
          type: constant
          maxAttempt: 3
          interval: PT10S

      - id: publish_update
        type: io.kestra.plugin.core.http.Request
        uri: "{{ inputs.callback_base_url }}/internal/activities/metrics/publish-update"
        method: POST
        contentType: application/json
        body: |
          {"status": "success"}
        options:
          connectTimeout: PT5S
          readTimeout: PT30S
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


def generate_periodic_flow_yaml(
    schedule: str,
    callback_base_url: str,
    concurrent: int = 5,
) -> str:
    """Generate the YAML source for a periodic metrics flow.

    The returned YAML contains Kestra ``{{ expression }}`` placeholders that
    Kestra evaluates at runtime — these are *not* Python format expressions.
    """
    return _PERIODIC_FLOW_TEMPLATE.substitute(
        flow_id=schedule_to_flow_id(schedule),
        schedule=schedule,
        callback_base_url=callback_base_url,
        concurrent=concurrent,
    )


async def sync_periodic_metrics_flows(
    kestra_client,
    db,
    callback_base_url: str,
    concurrent: int = 5,
) -> dict:
    """Sync periodic metrics flows in Kestra based on current metric definitions.

    Steps:
    1. Query distinct schedules from metric_definitions where is_active=True
       and schedule_cron IS NOT NULL.
    2. Generate one flow per unique schedule and register it via
       KestraClient.create_or_update_flow().
    3. Delete any metrics-periodic-* flows whose schedule is no longer
       represented in the definitions.

    Returns a dict with keys ``created``, ``deleted``, and ``unchanged``.
    """
    from sqlalchemy import distinct, select

    from src.shared.db.models import MetricDefinition

    # 1. Collect active schedules
    result = await db.execute(
        select(distinct(MetricDefinition.schedule_cron)).where(
            MetricDefinition.is_active == True,  # noqa: E712
            MetricDefinition.schedule_cron.isnot(None),
        )
    )
    active_schedules: set[str] = {row[0] for row in result.all() if row[0]}
    expected_flow_ids: set[str] = {schedule_to_flow_id(s) for s in active_schedules}

    # 2. Register a flow for every active schedule
    created: list[str] = []
    for schedule in active_schedules:
        flow_id = schedule_to_flow_id(schedule)
        flow_yaml = generate_periodic_flow_yaml(
            schedule, callback_base_url, concurrent=concurrent,
        )
        try:
            await kestra_client.create_or_update_flow(flow_yaml)
            created.append(flow_id)
            logger.info("Registered periodic metrics flow %s", flow_id)
        except Exception:
            logger.error(
                "Failed to register periodic metrics flow %s", flow_id, exc_info=True
            )

    # 3. Delete stale metrics-periodic-* flows
    deleted: list[str] = []
    try:
        existing_flows = await kestra_client.list_flows(prefix=PERIODIC_FLOW_PREFIX)
        existing_flow_ids: set[str] = {f["id"] for f in existing_flows}
        stale_flow_ids = existing_flow_ids - expected_flow_ids
        for flow_id in stale_flow_ids:
            try:
                await kestra_client.delete_flow(flow_id)
                deleted.append(flow_id)
                logger.info("Deleted stale periodic metrics flow %s", flow_id)
            except Exception:
                logger.error(
                    "Failed to delete stale metrics flow %s", flow_id, exc_info=True
                )
    except Exception:
        logger.error("Failed to list existing periodic metrics flows", exc_info=True)

    unchanged: list[str] = [
        fid for fid in expected_flow_ids if fid not in created
    ]

    return {"created": created, "deleted": deleted, "unchanged": unchanged}
