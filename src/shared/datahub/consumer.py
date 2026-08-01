"""Kafka consumer entry point for DataHub MetadataChangeLog events.

Subscribes to MCL topics, deserializes events, and routes them through
the EventRouter. Commits offsets only after successful processing.
Creates an Airflow client at startup so handlers can trigger DAG runs.

The consumer reads its whole Kafka connection — brokers plus the security
tuple — from the ``peripheral_config`` DB table at startup and re-checks every
~5 seconds.  When any element changes, the current consumer is closed and
rebuilt.  When the peripheral is unconfigured, the outer loop sleeps and
retries; a connection fault is reported to ``peripheral_health`` and retried
rather than exiting, so a misconfiguration can be corrected through the admin
API while the process runs.

Usage:
    python -m src.shared.datahub.consumer
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from confluent_kafka import Consumer, KafkaError

from src.shared.config import CONSUMER_POLL_TIMEOUT_S
from src.shared.datahub.events import build_router, deserialize_mcl
from src.shared.datahub.kafka_security import (
    KAFKA_CREDENTIAL_MECHANISMS,
    KAFKA_SASL_PROTOCOLS,
    check_kafka_security,
    derive_msk_region,
)
from src.shared.exceptions import EventProcessingError, KafkaConfigurationError
from src.shared.settings import settings

if TYPE_CHECKING:
    from src.workflows.airflow.client import AirflowClient

logger = structlog.get_logger(__name__)

MCL_TOPICS = [
    "MetadataChangeLog_Versioned_v1",
    "MetadataChangeLog_Timeseries_v1",
]

CONSUMER_GROUP_ID = "dataspoke-consumers"

_RECONFIG_CHECK_INTERVAL = 5  # poll iterations between connection-change checks
_UNCONFIGURED_SLEEP_S = 10.0
_FAULT_RETRY_SLEEP_S = 10.0
# Health is written on every state change and, when steady, at this cadence so a
# reader can tell a live consumer from one that stopped reporting.
_HEALTH_HEARTBEAT_S = 60.0

_REDACTED = "***"


@dataclass(frozen=True)
class KafkaConnection:
    """The DB-plane half of the consumer's Kafka connection.

    Equality across the whole tuple is what the poll loop compares: any change —
    including a ``sasl_password_version`` bump that stands in for a Secret-only
    rotation — rebuilds the client.
    """

    brokers: str
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str = ""
    sasl_username: str = ""
    aws_region: str = ""
    sasl_password_version: int = 0


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


async def read_kafka_connection() -> KafkaConnection | None:
    """Return the Kafka connection from peripheral_config, or None if unusable.

    Invalidates the process-level cache before reading so that configuration
    changes are visible within the 5-second reconfig check interval rather than
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
    if dto is None or not isinstance(dto, DatahubConfigDTO) or not dto.kafka_brokers:
        return None
    return KafkaConnection(
        brokers=dto.kafka_brokers,
        security_protocol=dto.kafka_security_protocol or "PLAINTEXT",
        sasl_mechanism=dto.kafka_sasl_mechanism,
        sasl_username=dto.kafka_sasl_username,
        aws_region=dto.kafka_aws_region,
        sasl_password_version=dto.kafka_sasl_password_version,
    )


def resolve_aws_region(conn: KafkaConnection) -> str:
    """Return the region used to sign an MSK IAM token.

    Explicit ``kafka_aws_region`` wins; otherwise the region is read off the MSK
    broker hostnames, matched anchored to the end of the host so that a hostile
    suffix cannot steer which region is signed for.  When neither resolves the
    consumer fails loudly instead of guessing a default, which would surface as
    an opaque authentication error.
    """
    if conn.aws_region:
        return conn.aws_region
    region = derive_msk_region(conn.brokers)
    if region is not None:
        return region
    raise KafkaConfigurationError(
        "AWS_MSK_IAM requires an AWS region: set kafka_aws_region on the DataHub "
        "peripheral, or use MSK broker hostnames of the form "
        "<broker>.kafka.<region>.amazonaws.com from which it can be derived"
    )


def _build_msk_oauth_cb(region: str) -> Callable[[str], tuple[str, float]]:
    """Return an OAUTHBEARER callback minting SigV4-signed MSK IAM tokens.

    The signer resolves whatever credentials the process has — on EKS, the pod's
    IRSA-projected role.  Failures (missing credentials, denied AssumeRole) are
    raised with the region named so the health report is actionable.
    """
    try:
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
    except ImportError as exc:  # pragma: no cover - the dependency ships in the image
        raise KafkaConfigurationError(
            "AWS_MSK_IAM requires the aws-msk-iam-sasl-signer-python package, "
            "which is missing from this image"
        ) from exc

    def oauth_cb(_config_str: str) -> tuple[str, float]:
        try:
            token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        except Exception as exc:
            logger.error("kafka_msk_iam_token_failed", region=region, error=str(exc))
            raise KafkaConfigurationError(
                f"AWS MSK IAM token generation failed for region '{region}': {exc}"
            ) from exc
        return token, expiry_ms / 1000.0

    return oauth_cb


def build_consumer_config(conn: KafkaConnection) -> dict[str, Any]:
    """Map a KafkaConnection onto confluent-kafka client properties.

    A PLAINTEXT connection carries no security properties at all, so an
    unsecured cluster needs nothing configured beyond the brokers.

    The stored tuple is re-validated here against the same rule engine the admin
    API applies.  ``peripheral_config.settings`` is untyped JSONB, so a row
    written by direct SQL or by a future writer that bypasses the request schema
    could hold a combination the API would have rejected — ``SASL_PLAINTEXT``
    with ``AWS_MSK_IAM``, say, which would put a SigV4 token from the pod's IAM
    identity on an unencrypted wire.  A rejected row raises rather than producing
    a client; the caller reports the reason to ``peripheral_health``.
    """
    violation = check_kafka_security(
        security_protocol=conn.security_protocol,
        sasl_mechanism=conn.sasl_mechanism,
        sasl_username=conn.sasl_username,
        aws_region=conn.aws_region,
        brokers=conn.brokers,
    )
    if violation is not None:
        raise KafkaConfigurationError(
            f"stored DataHub Kafka settings are invalid ({violation.field}): {violation.message}"
        )

    config: dict[str, Any] = {
        "bootstrap.servers": conn.brokers,
        "group.id": CONSUMER_GROUP_ID,
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
        "max.poll.interval.ms": 300000,
    }

    protocol = conn.security_protocol or "PLAINTEXT"
    if protocol == "PLAINTEXT":
        return config

    config["security.protocol"] = protocol
    if protocol not in KAFKA_SASL_PROTOCOLS:
        return config

    mechanism = conn.sasl_mechanism

    if mechanism == "AWS_MSK_IAM":
        # AWS implements MSK IAM as OAUTHBEARER whose token is a SigV4-signed
        # payload; security.protocol passes through as the stored value, which
        # the rule check above has already pinned to SASL_SSL.
        config["sasl.mechanism"] = "OAUTHBEARER"
        config["oauth_cb"] = _build_msk_oauth_cb(resolve_aws_region(conn))
        return config

    if mechanism not in KAFKA_CREDENTIAL_MECHANISMS:  # pragma: no cover - rules cover this
        raise KafkaConfigurationError(f"unsupported kafka_sasl_mechanism: {mechanism}")

    from src.backend.admin.datahub_secret import (
        get_datahub_kafka_sasl_password,
        invalidate_datahub_kafka_sasl_password_cache,
    )

    # The version counter has already told us the stored password may differ from
    # the cached one, so read through.
    invalidate_datahub_kafka_sasl_password_cache()
    password = get_datahub_kafka_sasl_password()
    if not password:
        # The accessor returns "" both when the key is absent and when RBAC denies
        # the read. Sending an empty password produces an opaque broker-side auth
        # failure; naming the local cause is what makes the health row actionable.
        raise KafkaConfigurationError(
            f"kafka_sasl_mechanism {mechanism} requires a kafka_sasl_password, but the "
            "kafka_sasl_password key of dataspoke-datahub-secret is empty or unreadable"
        )

    config["sasl.mechanism"] = mechanism
    config["sasl.username"] = conn.sasl_username
    config["sasl.password"] = password
    return config


class KafkaFaultState:
    """Latches librdkafka-level failures reported out of band by ``error_cb``.

    librdkafka surfaces authentication and connectivity failures on a background
    thread, where nothing can await a DB write; the async loop reads this object
    when it flushes health.

    The latch is **sticky**: a fault is held until positive evidence of recovery,
    never cleared merely by being read.  librdkafka re-emits ``ALL_BROKERS_DOWN``
    and ``_AUTHENTICATION`` on a backoff that grows to roughly ten seconds, so a
    read-and-clear latch drained every five seconds would land in the gap and
    report ``ok`` for a consumer that has never authenticated — precisely the
    state the health row exists to expose.
    """

    def __init__(self) -> None:
        self._error: str | None = None
        self._connected = False

    def record(self, message: str) -> bool:
        """Latch a connection fault; return True when it differs from the latched one.

        The return value is what callers log on, so that the "is this new?"
        judgement lives here rather than being re-derived by each caller.
        librdkafka re-emits an unchanged fault on its reconnect backoff — roughly
        nineteen times a second for ``ALL_BROKERS_DOWN`` — and logging every
        callback buries the surrounding lines an operator needs during exactly
        the outage that produced them.
        """
        is_new = message != self._error
        self._error = message
        return is_new

    def record_healthy(self) -> str | None:
        """Note positive evidence of a working connection: assignment or a live message.

        Returns the fault this cleared, or None when nothing was latched — so a
        caller can log a recovery without also logging on every healthy message
        or on the first connection, neither of which is a recovery.
        """
        cleared = self._error
        self._error = None
        self._connected = True
        return cleared

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def connected(self) -> bool:
        return self._connected


_FAULT_CODES = {KafkaError._AUTHENTICATION, KafkaError._ALL_BROKERS_DOWN}  # noqa: SLF001


def _note_healthy(state: KafkaFaultState) -> None:
    """Record positive evidence of a working connection, logging a genuine recovery."""
    cleared = state.record_healthy()
    if cleared is not None:
        logger.info("kafka_connection_recovered", previous_error=cleared)


def _make_error_cb(state: KafkaFaultState) -> Callable[[KafkaError], None]:
    # Transient errors are not latched, so their repeat-suppression is local:
    # librdkafka re-emits per-broker transport failures on the same backoff that
    # drives the latched faults.
    last_transient: str | None = None

    def error_cb(err: KafkaError) -> None:
        nonlocal last_transient
        message = str(err)
        if err.code() in _FAULT_CODES:
            if state.record(message):
                logger.error("kafka_connection_fault", error=message)
            last_transient = None
        else:
            if message != last_transient:
                logger.warning("kafka_client_error", error=message)
            last_transient = message

    return error_cb


class HealthReporter:
    """Writes the ``datahub`` peripheral_health row on state change or heartbeat.

    Holds the credential currently on the wire so it can be scrubbed out of any
    message before it is persisted.  librdkafka is not observed to echo the SASL
    password in its error strings, but ``str(err)`` is stored verbatim in a row an
    Admin reads back, so the guarantee is made here rather than inherited.
    """

    def __init__(self) -> None:
        self._last_status: str | None = None
        self._last_error: str | None = None
        self._last_written_at: float = 0.0
        self._redact: str | None = None

    def set_redaction(self, secret: str | None) -> None:
        """Register the credential to scrub from reported messages."""
        self._redact = secret or None

    def _scrub(self, message: str | None) -> str | None:
        if message and self._redact:
            return message.replace(self._redact, _REDACTED)
        return message

    async def report(self, status: str, error: str | None = None) -> None:
        error = self._scrub(error)
        now = time.monotonic()
        unchanged = status == self._last_status and error == self._last_error
        if unchanged and now - self._last_written_at < _HEALTH_HEARTBEAT_S:
            return

        from src.backend.admin.peripheral_health import report_peripheral_health
        from src.shared.db.session import SessionLocal

        try:
            async with SessionLocal() as db:
                await report_peripheral_health(db, "datahub", status, error)
        except Exception:
            # Health reporting is observability, never a reason to stop consuming.
            logger.warning("peripheral_health_report_failed", status=status)
            return

        self._last_status = status
        self._last_error = error
        self._last_written_at = now


async def _run_inner_loop(
    consumer: Consumer,
    router: object,
    current_conn: KafkaConnection,
    faults: KafkaFaultState,
    health: HealthReporter,
) -> None:
    """Poll messages until the connection settings change, then return to rebuild.

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
                # A committed message is proof the connection authenticated.
                _note_healthy(faults)
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
            # A sticky fault outranks everything; ``ok`` requires positive
            # evidence (partition assignment or a committed message). Neither
            # holding means nothing is known yet, so the row is left alone rather
            # than being asserted healthy.
            if faults.error is not None:
                await health.report("error", faults.error)
            elif faults.connected:
                await health.report("ok")

            new_conn = await read_kafka_connection()
            if new_conn != current_conn:
                logger.info(
                    "consumer_connection_changed",
                    old_brokers=current_conn.brokers,
                    new_brokers=new_conn.brokers if new_conn else None,
                )
                return


async def run_consumer() -> None:
    """Main consumer loop — subscribe, poll, route, commit.

    Outer loop: reads peripheral_config and builds a Consumer when configured.
    Inner loop: polls messages; rebuilds when the connection settings change.
    A connection or configuration fault is reported and retried, never fatal.
    """
    airflow_client = _create_airflow_client()
    router = build_router(airflow_client=airflow_client)
    health = HealthReporter()

    while True:
        try:
            conn = await read_kafka_connection()
        except Exception as exc:
            # This read reaches the same database every other segment of the loop
            # already treats as fallible. On a fresh install the API's Alembic
            # init container has not yet created `peripheral_config`; mid-life a
            # Postgres blip looks the same. Neither is a reason to exit and be
            # restarted.
            logger.error("consumer_config_read_failed", error=str(exc))
            await health.report("error", str(exc))
            await asyncio.sleep(_FAULT_RETRY_SLEEP_S)
            continue

        if conn is None:
            logger.info("consumer_waiting_for_config", retry_in_s=_UNCONFIGURED_SLEEP_S)
            await asyncio.sleep(_UNCONFIGURED_SLEEP_S)
            continue

        faults = KafkaFaultState()
        try:
            config = build_consumer_config(conn)
        except KafkaConfigurationError as exc:
            logger.error("consumer_config_invalid", error=str(exc))
            await health.report("error", str(exc))
            await asyncio.sleep(_FAULT_RETRY_SLEEP_S)
            continue

        config["error_cb"] = _make_error_cb(faults)
        # Whatever credential this client carries must never reach the health row.
        health.set_redaction(config.get("sasl.password"))

        def _on_assign(_consumer: Consumer, partitions: list[Any]) -> None:
            # Partition assignment means the group join succeeded, which requires
            # a completed SASL handshake — positive evidence, without waiting for
            # traffic on a quiet topic.
            _note_healthy(faults)
            logger.info("consumer_assigned", partitions=len(partitions))

        try:
            consumer = Consumer(config)
            consumer.subscribe(MCL_TOPICS, on_assign=_on_assign)
        except Exception as exc:
            logger.error("consumer_start_failed", error=str(exc))
            await health.report("error", str(exc))
            await asyncio.sleep(_FAULT_RETRY_SLEEP_S)
            continue

        logger.info(
            "consumer_started",
            topics=MCL_TOPICS,
            brokers=conn.brokers,
            security_protocol=conn.security_protocol,
            sasl_mechanism=conn.sasl_mechanism or None,
        )

        try:
            await _run_inner_loop(consumer, router, conn, faults, health)
        except Exception as exc:
            logger.exception("consumer_loop_failed")
            await health.report("error", str(exc))
            await asyncio.sleep(_FAULT_RETRY_SLEEP_S)
        finally:
            consumer.close()
            logger.info("consumer_stopped")


if __name__ == "__main__":
    asyncio.run(run_consumer())
