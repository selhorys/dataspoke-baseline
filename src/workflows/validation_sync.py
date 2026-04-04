"""Validation workflow — periodic flow generation and sync logic.

Periodic validation is handled by dynamically generated Kestra flows,
one per unique cron schedule, managed by the validation-config-sync flow.
"""

from __future__ import annotations

import hashlib
import logging
import string

logger = logging.getLogger(__name__)

FLOW_PREFIX = "validation-periodic-"

# Kestra YAML template for periodic validation flows.
# Uses $-style placeholders (string.Template) to avoid conflicts with
# Kestra's {{ }} expression syntax, which must pass through verbatim.
_PERIODIC_FLOW_TEMPLATE = string.Template(
    """\
id: $flow_id
namespace: dataspoke
description: "Periodic validation for schedule: $schedule"

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

tasks:
  - id: list_datasets
    type: io.kestra.plugin.core.http.Request
    uri: "{{ inputs.callback_base_url }}/internal/activities/validation/list-periodic"
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
    value: "{{ outputs.list_datasets.body }}"
    concurrent: $concurrent
    tasks:
      - id: run_validation
        type: io.kestra.plugin.core.http.Request
        uri: "{{ inputs.callback_base_url }}/internal/activities/validation/run"
        method: POST
        contentType: application/json
        body: |
          {"dataset_urn": "{{ taskrun.value }}"}
        options:
          connectTimeout: PT5S
          readTimeout: PT30S
        retry:
          type: constant
          maxAttempt: 3
          interval: PT10S
"""
)


def schedule_to_flow_id(cron: str) -> str:
    """Return the Kestra flow ID for a given cron schedule string.

    Uses the first 8 hex chars of the MD5 hash of the cron expression for a
    stable, human-readable short identifier.
    """
    digest = hashlib.md5(cron.encode()).hexdigest()[:8]  # noqa: S324
    return f"{FLOW_PREFIX}{digest}"


def generate_periodic_flow_yaml(
    cron: str,
    callback_base_url: str,
    concurrent: int = 5,
) -> str:
    """Generate the YAML source for a periodic validation flow.

    The returned YAML contains Kestra ``{{ expression }}`` placeholders that
    Kestra evaluates at runtime — these are *not* Python format expressions.
    """
    return _PERIODIC_FLOW_TEMPLATE.substitute(
        flow_id=schedule_to_flow_id(cron),
        schedule=cron,
        callback_base_url=callback_base_url,
        concurrent=concurrent,
    )


async def sync_periodic_validation_flows(
    kestra_client,
    db,
    callback_base_url: str,
    concurrent: int = 5,
) -> dict:
    """Sync periodic validation flows in Kestra based on current configs.

    Steps:
    1. Query distinct cron values from validation_configs where is_active=true
       and schedule_cron IS NOT NULL.
    2. Generate one flow per unique cron and register it via
       KestraClient.create_or_update_flow().
    3. Delete any validation-periodic-* flows whose cron is no longer
       represented in the configs.

    Returns a dict with keys ``created``, ``deleted``, and ``unchanged``.
    """
    from sqlalchemy import func, select

    from src.shared.db.models import ValidationConfig

    # 1. Collect distinct active cron schedules
    result = await db.execute(
        select(func.distinct(ValidationConfig.schedule_cron)).where(
            ValidationConfig.is_active == True,  # noqa: E712
            ValidationConfig.schedule_cron.isnot(None),
        )
    )
    active_crons: set[str] = {row[0] for row in result.all() if row[0]}
    expected_flow_ids: set[str] = {schedule_to_flow_id(c) for c in active_crons}

    # 2. Register a flow for every active cron
    created: list[str] = []
    for cron in active_crons:
        flow_id = schedule_to_flow_id(cron)
        flow_yaml = generate_periodic_flow_yaml(
            cron, callback_base_url, concurrent=concurrent,
        )
        try:
            await kestra_client.create_or_update_flow(flow_yaml)
            created.append(flow_id)
            logger.info("Registered periodic validation flow %s", flow_id)
        except Exception:
            logger.error(
                "Failed to register periodic validation flow %s", flow_id, exc_info=True
            )

    # 3. Delete stale validation-periodic-* flows
    deleted: list[str] = []
    try:
        existing_flows = await kestra_client.list_flows(prefix=FLOW_PREFIX)
        existing_flow_ids: set[str] = {f["id"] for f in existing_flows}
        stale_flow_ids = existing_flow_ids - expected_flow_ids
        for flow_id in stale_flow_ids:
            try:
                await kestra_client.delete_flow(flow_id)
                deleted.append(flow_id)
                logger.info("Deleted stale periodic validation flow %s", flow_id)
            except Exception:
                logger.error(
                    "Failed to delete stale validation flow %s", flow_id, exc_info=True
                )
    except Exception:
        logger.error("Failed to list existing periodic validation flows", exc_info=True)

    unchanged: list[str] = [
        fid for fid in expected_flow_ids if fid not in created
    ]

    return {"created": created, "deleted": deleted, "unchanged": unchanged}
