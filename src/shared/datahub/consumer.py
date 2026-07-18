"""Kafka consumer entry point for DataHub MetadataChangeLog events.

Subscribes to MCL topics, deserializes events, and routes them through
the EventRouter. Commits offsets only after successful processing.
Creates an Airflow client at startup so handlers can trigger DAG runs.

The consumer reads its Kafka broker address from the ``peripheral_config``
DB table at startup and re-checks every ~5 seconds.  When the address
changes, the current consumer is closed and rebuilt against the new brokers.
When the peripheral is unconfigured, the outer loop sleeps and retries.

Usage:
    python -m src.shared.datahub.consumer
"""

import asyncio
from typing import TYPE_CHECKING

import structlog
from confluent_kafka import Consumer, KafkaError

from src.shared.config import CONSUMER_POLL_TIMEOUT_S
from src.shared.datahub.events import build_router, deserialize_mcl
from src.shared.exceptions import EventProcessingError
from src.shared.settings import settings

if TYPE_CHECKING:
    from src.workflows.airflow.client import AirflowClient

logger = structlog.get_logger(__name__)

MCL_TOPICS = [
    "MetadataChangeLog_Versioned_v1",
    "MetadataChangeLog_Timeseries_v1",
]

_RECONFIG_CHECK_INTERVAL = 5  # poll iterations between broker-change checks
_UNCONFIGURED_SLEEP_S = 10.0


def _create_airflow_client() -> "AirflowClient | None":
    """Create an Airflow client; return None if configuration is missing."""
    try:
        from src.workflows.airflow.client import AirflowClient

        client = AirflowClient(
            base_url=settings.airflow_url,
            username=settings.airflow_user,
            password=settings.airflow_password,
        )
        logger.info("airflow_client_created", url=settings.airflow_url)
        return client
    except Exception:
        logger.warning(
            "airflow_unavailable",
            msg="handlers requiring Airflow will be no-ops",
        )
        return None


async def _read_kafka_brokers() -> str | None:
    """Return the kafka_brokers value from peripheral_config, or None if absent.

    Invalidates the process-level cache before reading so that broker changes
    are visible within the 5-second reconfig check interval rather than
    being masked by the 30-second cache TTL.
    """
    from src.backend.admin.peripheral_service import (
        DatahubConfigDTO,
        get_peripheral_config,
        invalidate_peripheral_config_cache,
    )
    from src.shared.db.session import SessionLocal

    invalidate_peripheral_config_cache("datahub")
    async with SessionLocal() as db:
        dto = await get_peripheral_config(db, "datahub")
    if dto is None or not isinstance(dto, DatahubConfigDTO):
        return None
    return dto.kafka_brokers or None


async def _run_inner_loop(consumer: Consumer, router: object, current_brokers: str) -> None:
    """Poll messages until brokers change, then return so the outer loop rebuilds.

    The outer loop calls ``consumer.close()`` in a ``finally`` block after this
    returns, regardless of the reason for returning.
    """
    poll_count = 0
    while True:
        msg = await asyncio.to_thread(consumer.poll, CONSUMER_POLL_TIMEOUT_S)

        if msg is None:
            pass
        elif msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:  # type: ignore[union-attr]  # guarded by msg.error() check above.
                pass
            else:
                logger.warning("consumer_error", error=str(msg.error()))
        else:
            try:
                event = deserialize_mcl(msg.value())  # type: ignore[arg-type]  # non-None on this path (error checked above).
                await router.dispatch(event)  # type: ignore[attr-defined]
                consumer.commit(message=msg)
            except EventProcessingError:
                logger.exception(
                    "event_deserialization_failed",
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                )
                consumer.commit(message=msg)
            except Exception:
                logger.exception(
                    "event_processing_failed",
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                )

        poll_count += 1
        if poll_count % _RECONFIG_CHECK_INTERVAL == 0:
            new_brokers = await _read_kafka_brokers()
            if new_brokers != current_brokers:
                logger.info(
                    "consumer_brokers_changed",
                    old=current_brokers,
                    new=new_brokers,
                )
                return


async def run_consumer() -> None:
    """Main consumer loop — subscribe, poll, route, commit.

    Outer loop: reads peripheral_config and builds a Consumer when configured.
    Inner loop: polls messages; rebuilds when kafka_brokers changes.
    """
    airflow_client = _create_airflow_client()
    router = build_router(airflow_client=airflow_client)

    while True:
        brokers = await _read_kafka_brokers()
        if brokers is None:
            logger.info("consumer_waiting_for_config", retry_in_s=_UNCONFIGURED_SLEEP_S)
            await asyncio.sleep(_UNCONFIGURED_SLEEP_S)
            continue

        consumer = Consumer(
            {
                "bootstrap.servers": brokers,
                "group.id": "dataspoke-consumers",
                "auto.offset.reset": "latest",
                "enable.auto.commit": False,
                "max.poll.interval.ms": 300000,
            }
        )
        consumer.subscribe(MCL_TOPICS)
        logger.info("consumer_started", topics=MCL_TOPICS, brokers=brokers)

        try:
            await _run_inner_loop(consumer, router, brokers)
        finally:
            consumer.close()
            logger.info("consumer_stopped")


if __name__ == "__main__":
    asyncio.run(run_consumer())
