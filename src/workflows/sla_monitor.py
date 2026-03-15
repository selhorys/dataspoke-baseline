"""SLA monitor workflow — periodic SLA checks with alerting."""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from src.backend.validation.service import ValidationService
    from src.backend.validation.sla import check_sla
    from src.shared.config import SLA_MONITOR_INTERVAL_MINUTES
    from src.shared.db.session import SessionLocal
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        default_retry_policy,
        make_cache,
        make_datahub,
        make_notification,
    )


@dataclass
class SLAMonitorParams:
    dataset_urn: str
    sla_target: dict
    alert_recipients: list[str] = field(default_factory=list)


@activity.defn
async def check_sla_activity(dataset_urn: str, sla_target: dict) -> dict:
    """Check SLA compliance for a single dataset."""
    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    qdrant = make_qdrant()

    async with SessionLocal() as db:
        # Get quality score via validation service
        service = ValidationService(datahub=datahub, db=db, cache=cache, llm=llm, qdrant=qdrant)
        quality_score = 0.0
        try:
            results, _ = await service.get_results(dataset_urn, limit=1)
            if results:
                quality_score = results[0].quality_score
        except Exception:
            pass

    # Get dataset profile history for freshness analysis
    from datahub.metadata.schema_classes import DatasetProfileClass

    history = await datahub.get_timeseries(dataset_urn, DatasetProfileClass, limit=30)

    result = await check_sla(
        datahub=datahub,
        dataset_urn=dataset_urn,
        sla_target=sla_target,
        history=history,
        quality_score=quality_score,
    )

    alerts = []
    if result.is_breaching or result.is_pre_breach:
        alerts.append(
            {
                "dataset_urn": dataset_urn,
                "is_breaching": result.is_breaching,
                "is_pre_breach": result.is_pre_breach,
                "violations": result.violations,
                "predicted_breach_at": (
                    result.predicted_breach_at.isoformat() if result.predicted_breach_at else None
                ),
            }
        )

    return {
        "dataset_urn": dataset_urn,
        "is_breaching": result.is_breaching,
        "is_pre_breach": result.is_pre_breach,
        "freshness_hours": result.current_freshness_hours,
        "quality_score": result.current_quality_score,
        "violations": result.violations,
        "alerts": alerts,
    }


@activity.defn
async def send_sla_alerts_activity(alerts: list[dict], recipients: list[str]) -> None:
    """Send SLA alert notifications."""
    from datetime import UTC, datetime

    from src.shared.notifications.models import SLAAlert

    notification = make_notification()

    for alert_data in alerts:
        predicted_str = alert_data.get("predicted_breach_at")
        predicted_dt = (
            datetime.fromisoformat(predicted_str) if predicted_str else datetime.now(tz=UTC)
        )
        alert = SLAAlert(
            dataset_urn=alert_data["dataset_urn"],
            sla_name="freshness",
            predicted_breach_at=predicted_dt,
            root_cause="; ".join(alert_data.get("violations", [])),
            recommended_actions=["Investigate upstream pipelines"],
        )
        await notification.send_sla_alert(recipients, alert)


@workflow.defn
class SLAMonitorWorkflow:
    """Periodic SLA monitoring with continue-as-new for long-running checks.

    Workflow ID convention: ``sla-monitor-{dataset_urn}``
    """

    @workflow.run
    async def run(self, params: SLAMonitorParams) -> None:
        sla_result = await workflow.execute_activity(
            check_sla_activity,
            args=[params.dataset_urn, params.sla_target],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        if sla_result.get("alerts") and params.alert_recipients:
            await workflow.execute_activity(
                send_sla_alerts_activity,
                args=[sla_result["alerts"], params.alert_recipients],
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=default_retry_policy(),
            )

        await workflow.sleep(timedelta(minutes=SLA_MONITOR_INTERVAL_MINUTES))
        workflow.continue_as_new(params)
