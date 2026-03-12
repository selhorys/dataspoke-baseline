"""Unit tests for SLAMonitorWorkflow using Temporal test framework."""

import asyncio

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.sla_monitor import SLAMonitorParams, SLAMonitorWorkflow

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"
_SLA_TARGET = {"freshness_hours": 24, "min_quality_score": 70.0}


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_no_breach_no_alerts(env: WorkflowEnvironment):
    alert_called = False
    check_done = asyncio.Event()

    @activity.defn(name="check_sla_activity")
    async def mock_check(dataset_urn: str, sla_target: dict) -> dict:
        check_done.set()
        return {
            "dataset_urn": dataset_urn,
            "is_breaching": False,
            "is_pre_breach": False,
            "freshness_hours": 2.0,
            "quality_score": 90.0,
            "violations": [],
            "alerts": [],
        }

    @activity.defn(name="send_sla_alerts_activity")
    async def mock_alert(alerts: list[dict], recipients: list[str]) -> None:
        nonlocal alert_called
        alert_called = True

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[SLAMonitorWorkflow],
        activities=[mock_check, mock_alert],
    ):
        handle = await env.client.start_workflow(
            SLAMonitorWorkflow.run,
            SLAMonitorParams(
                dataset_urn=_DATASET_URN,
                sla_target=_SLA_TARGET,
                alert_recipients=["ops@example.com"],
            ),
            id="sla-test-1",
            task_queue=TASK_QUEUE,
        )
        await asyncio.wait_for(check_done.wait(), timeout=10)
        await handle.cancel()

    assert not alert_called


async def test_breach_sends_alert(env: WorkflowEnvironment):
    alert_done = asyncio.Event()

    @activity.defn(name="check_sla_activity")
    async def mock_check(dataset_urn: str, sla_target: dict) -> dict:
        return {
            "dataset_urn": dataset_urn,
            "is_breaching": True,
            "is_pre_breach": False,
            "freshness_hours": 48.0,
            "quality_score": 50.0,
            "violations": ["Freshness breach: 48h exceeds 24h limit"],
            "alerts": [
                {
                    "dataset_urn": dataset_urn,
                    "is_breaching": True,
                    "is_pre_breach": False,
                    "violations": ["Freshness breach: 48h exceeds 24h limit"],
                    "predicted_breach_at": None,
                }
            ],
        }

    @activity.defn(name="send_sla_alerts_activity")
    async def mock_alert(alerts: list[dict], recipients: list[str]) -> None:
        assert len(alerts) == 1
        assert recipients == ["ops@example.com"]
        alert_done.set()

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[SLAMonitorWorkflow],
        activities=[mock_check, mock_alert],
    ):
        handle = await env.client.start_workflow(
            SLAMonitorWorkflow.run,
            SLAMonitorParams(
                dataset_urn=_DATASET_URN,
                sla_target=_SLA_TARGET,
                alert_recipients=["ops@example.com"],
            ),
            id="sla-test-2",
            task_queue=TASK_QUEUE,
        )
        await asyncio.wait_for(alert_done.wait(), timeout=10)
        await handle.cancel()
