"""Unit tests for the consumer's Kafka client config and health reporting.

Three concerns, all exercised without a broker, a database, or a Kubernetes API:

1. ``build_consumer_config`` — the wire-level mapping of the stored security tuple onto
   ``confluent-kafka`` client properties, plus the re-validation the consumer performs
   against the stored row before it builds a client at all.
2. ``KafkaFaultState`` / ``HealthReporter`` / the inner loop's health flush — the
   ``peripheral_health`` signal that makes an unreachable Kafka visible over HTTP.
3. Log-repeat suppression in ``_make_error_cb`` / ``_note_healthy`` — one line per
   distinct fault rather than one per librdkafka callback. This section pins a defect
   found in live cluster operation; see its header comment.

Spec traceability:
- spec/feature/BACKEND.md §Kafka connection — the peripheral-field → client-property
  table; the ``AWS_MSK_IAM`` → ``OAUTHBEARER`` mapping with ``security.protocol``
  passing through; the region fallback and "fails loudly"; "**The consumer
  re-validates the protocol/mechanism combination when it builds a client**".
- spec/feature/BACKEND.md §Health reporting — "``ok`` once subscribed and polling,
  ``error`` with the message on a connection or authentication failure"; ``unknown``
  covers "never reported".
- spec/API.md §DataHub Kafka security — the seven rules, re-asserted here against storage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from confluent_kafka import KafkaError

from src.shared.datahub.consumer import (
    CONSUMER_GROUP_ID,
    KafkaConnection,
    build_consumer_config,
    resolve_aws_region,
)
from src.shared.exceptions import KafkaConfigurationError

_MSK_BROKERS = (
    "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098,"
    "b-2.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098"
)

# The exact property set an unsecured connection produces. Pinned literally because
# byte-for-byte equality with the pre-Kafka-security config is the backward-compatibility
# guarantee: an existing PLAINTEXT deployment must keep the client it already had.
_PLAINTEXT_CONFIG = {
    "bootstrap.servers": "kafka:9092",
    "group.id": CONSUMER_GROUP_ID,
    "auto.offset.reset": "latest",
    "enable.auto.commit": False,
    "max.poll.interval.ms": 300000,
}


def _patch_password(value: str):
    """Patch the lazily imported Kafka SASL password accessor."""
    return (
        patch(
            "src.backend.admin.datahub_secret.get_datahub_kafka_sasl_password",
            return_value=value,
        ),
        patch(
            "src.backend.admin.datahub_secret.invalidate_datahub_kafka_sasl_password_cache",
            MagicMock(),
        ),
    )


# ── PLAINTEXT: no security properties at all ─────────────────────────────────


@pytest.mark.parametrize(
    ("protocol", "label"),
    [
        ("", "absent — a row predating the Kafka security tuple"),
        ("PLAINTEXT", "explicitly stored PLAINTEXT"),
    ],
)
def test_plaintext_config_is_exactly_the_five_base_properties(
    protocol: str, label: str
) -> None:
    """An unsecured connection carries no security properties whatsoever.

    The dict is compared for equality, not containment: an added ``security.protocol``
    or ``sasl.*`` key would change the client an existing PLAINTEXT deployment gets.

    spec: feature/BACKEND.md §Kafka connection — the client-property table; "A PLAINTEXT
    connection carries no security properties at all"; spec/API.md §DataHub Kafka
    security — ``PLAINTEXT`` is the default and "All of it is optional".
    """
    conn = KafkaConnection(brokers="kafka:9092", security_protocol=protocol)
    assert build_consumer_config(conn) == _PLAINTEXT_CONFIG, f"protocol {label}"


def test_plaintext_config_uses_the_shared_consumer_group() -> None:
    """The consumer joins the single ``dataspoke-consumers`` group.

    spec: feature/BACKEND.md §Kafka Consumers — "DataSpoke runs a single consumer group
    (``dataspoke-consumers``)".
    """
    assert CONSUMER_GROUP_ID == "dataspoke-consumers"


def test_plaintext_config_commits_offsets_manually() -> None:
    """Auto-commit stays off so a failed handler leaves the offset uncommitted.

    spec: feature/BACKEND.md §Kafka Consumers — "Uses ``confluent-kafka`` with manual
    offset commit … handler failures leave the offset uncommitted for redelivery".
    """
    config = build_consumer_config(KafkaConnection(brokers="kafka:9092"))
    assert config["enable.auto.commit"] is False


# ── SSL: transport security without SASL ─────────────────────────────────────


def test_ssl_config_sets_the_protocol_and_no_sasl_properties() -> None:
    """``SSL`` maps to ``security.protocol`` alone — there is no credential to carry.

    spec: feature/BACKEND.md §Kafka connection — ``kafka_security_protocol`` →
    ``security.protocol``; spec/API.md §DataHub Kafka security rule 1 — a mechanism is
    rejected with ``SSL``.
    """
    config = build_consumer_config(
        KafkaConnection(brokers="kafka:9093", security_protocol="SSL")
    )
    assert config == {**_PLAINTEXT_CONFIG, "bootstrap.servers": "kafka:9093",
                      "security.protocol": "SSL"}


# ── SASL credential mechanisms ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("protocol", "mechanism"),
    [
        ("SASL_SSL", "PLAIN"),
        ("SASL_SSL", "SCRAM-SHA-256"),
        ("SASL_SSL", "SCRAM-SHA-512"),
        ("SASL_PLAINTEXT", "SCRAM-SHA-512"),
    ],
)
def test_credential_mechanism_maps_to_sasl_properties(protocol: str, mechanism: str) -> None:
    """The username comes from the DB row and the password from the K8s Secret.

    spec: feature/BACKEND.md §Kafka connection — ``kafka_sasl_mechanism`` (``PLAIN``,
    ``SCRAM-SHA-*``) → ``sasl.mechanism``; ``kafka_sasl_username`` / the
    ``kafka_sasl_password`` Secret key → ``sasl.username`` / ``sasl.password``.
    """
    conn = KafkaConnection(
        brokers="kafka:9093",
        security_protocol=protocol,
        sasl_mechanism=mechanism,
        sasl_username="dataspoke",
    )
    get_pw, invalidate = _patch_password("s3cr3t")
    with get_pw, invalidate:
        config = build_consumer_config(conn)

    assert config == {
        **_PLAINTEXT_CONFIG,
        "bootstrap.servers": "kafka:9093",
        "security.protocol": protocol,
        "sasl.mechanism": mechanism,
        "sasl.username": "dataspoke",
        "sasl.password": "s3cr3t",
    }


def test_credential_mechanism_reads_the_password_through_the_cache() -> None:
    """The Secret is re-read rather than served from the process cache.

    The version counter has already told the loop the stored password may differ from
    the cached one, so building a client must not reuse a stale entry.

    spec: feature/BACKEND.md §Kafka connection — "``kafka_sasl_password_version`` exists
    because a rotated password is invisible in the DB row … which turns a rotation into
    an ordinary DB-plane change the poll loop already detects."
    """
    conn = KafkaConnection(
        brokers="kafka:9093",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username="dataspoke",
    )
    with (
        patch(
            "src.backend.admin.datahub_secret.get_datahub_kafka_sasl_password",
            return_value="rotated",
        ),
        patch(
            "src.backend.admin.datahub_secret.invalidate_datahub_kafka_sasl_password_cache"
        ) as mock_invalidate,
    ):
        config = build_consumer_config(conn)

    mock_invalidate.assert_called_once_with()
    # Backstop: the fresh read is what reached the client config.
    assert config["sasl.password"] == "rotated"


def test_credential_mechanism_with_an_empty_password_raises_instead_of_connecting() -> None:
    """An unset or unreadable Kafka password is a configuration error, not a blank login.

    Sending an empty password produces an opaque broker-side authentication failure; the
    consumer names the local cause so the health row is actionable.

    spec: feature/BACKEND.md §Health reporting — the health row exists so a bad
    credential is not "warnings nobody reads"; spec/API.md §DataHub Kafka security —
    a credential mechanism's password lives in ``dataspoke-datahub-secret``.
    """
    conn = KafkaConnection(
        brokers="kafka:9093",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username="dataspoke",
    )
    get_pw, invalidate = _patch_password("")
    with get_pw, invalidate:
        with pytest.raises(KafkaConfigurationError, match="kafka_sasl_password"):
            build_consumer_config(conn)


# ── AWS_MSK_IAM ──────────────────────────────────────────────────────────────


def _msk_conn(**overrides) -> KafkaConnection:
    base = {
        "brokers": _MSK_BROKERS,
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "AWS_MSK_IAM",
    }
    base.update(overrides)
    return KafkaConnection(**base)


def test_msk_iam_maps_to_oauthbearer_with_the_stored_protocol_passing_through() -> None:
    """``AWS_MSK_IAM`` becomes ``OAUTHBEARER`` on the wire; ``SASL_SSL`` is unchanged.

    ``AWS_MSK_IAM`` is not a librdkafka mechanism — AWS implements it as OAUTHBEARER
    whose token is a SigV4-signed payload.

    spec: feature/BACKEND.md §Kafka connection — "``kafka_sasl_mechanism = AWS_MSK_IAM``
    → ``sasl.mechanism=OAUTHBEARER`` plus a token-refresh callback; ``security.protocol``
    passes through as the stored ``SASL_SSL``".
    """
    config = build_consumer_config(_msk_conn())

    assert config["sasl.mechanism"] == "OAUTHBEARER"
    assert config["security.protocol"] == "SASL_SSL"
    assert callable(config["oauth_cb"]), "a token-refresh callback must be installed"
    # No typed credential participates in an IAM connection.
    assert "sasl.username" not in config
    assert "sasl.password" not in config


def test_msk_iam_does_not_read_the_kafka_password_secret() -> None:
    """An IAM connection never touches the SASL password.

    spec: spec/API.md §DataHub Kafka security rule 3 — ``AWS_MSK_IAM`` "authenticates
    with the consumer pod's IAM identity", so a stored password "has lost its purpose".
    """
    with patch(
        "src.backend.admin.datahub_secret.get_datahub_kafka_sasl_password",
        side_effect=AssertionError("the Kafka password must not be read under AWS_MSK_IAM"),
    ):
        config = build_consumer_config(_msk_conn())

    assert config["sasl.mechanism"] == "OAUTHBEARER"


def test_resolve_region_uses_the_explicit_setting_when_present() -> None:
    """``kafka_aws_region`` is consulted first; the hostname is the fallback.

    Both sources agree here, because rule 7 rejects a stored tuple in which they do not —
    so this is the only shape of "explicit region present" the consumer can actually
    receive.

    spec: feature/BACKEND.md §Kafka connection — "It comes from ``kafka_aws_region`` when
    set, otherwise from the broker hostname"; spec/API.md §DataHub Kafka security rule 7 —
    "When ``kafka_aws_region`` is set and the broker hosts encode a region, the two must
    agree".
    """
    assert resolve_aws_region(_msk_conn(aws_region="us-east-1")) == "us-east-1"


def test_resolve_region_precedence_holds_even_where_rule7_would_forbid_the_tuple() -> None:
    """Defence in depth: the explicit region wins outright, not by coincidence.

    Rule 7 now rejects a region contradicting the hosts, so ``check_kafka_security``
    prevents this tuple from ever reaching ``resolve_aws_region`` through the API or the
    consumer's re-validation. The precedence contract is pinned anyway: this function is
    public, and a future caller that skipped the rule check must still get the operator's
    stated region rather than a silently derived one.

    Deliberately pins an unreachable-by-construction input — flagged here so a reader does
    not mistake it for a supported configuration.

    spec: feature/BACKEND.md §Kafka connection — the region "comes from
    ``kafka_aws_region`` when set"; spec/API.md §DataHub Kafka security rule 7 — why the
    combination cannot be stored.
    """
    # The brokers encode us-east-1; the explicit setting still takes precedence.
    assert resolve_aws_region(_msk_conn(aws_region="eu-west-1")) == "eu-west-1"


def test_resolve_region_falls_back_to_the_broker_hostname() -> None:
    """With no explicit region the MSK hostname supplies it.

    spec: feature/BACKEND.md §Kafka connection — "otherwise from the broker hostname,
    which for MSK encodes it (``…c2.kafka.<region>.amazonaws.com``)".
    """
    assert resolve_aws_region(_msk_conn(aws_region="")) == "us-east-1"


@pytest.mark.parametrize(
    "brokers",
    [
        pytest.param("kafka.amazonaws.com:9098", id="msk-domain-with-no-region"),
        pytest.param(
            "ec2-203-0-113-25.compute-1.amazonaws.com:9098", id="aws-host-that-is-not-a-broker"
        ),
        pytest.param("kafka.internal.corp:9093", id="wholly-foreign-host"),
    ],
)
def test_resolve_region_fails_loudly_when_neither_source_resolves(brokers: str) -> None:
    """No explicit region and no derivable one is an error, not a guessed default.

    A guessed region would surface as an opaque authentication failure at the broker.

    Rule 6 now rejects every one of these broker strings, so ``build_consumer_config``
    refuses the row before reaching this guard; it is retained as defence in depth for a
    caller that reaches ``resolve_aws_region`` directly, and asserted here on the
    function's own contract rather than as a reachable pipeline state.

    spec: feature/BACKEND.md §Kafka connection — "When neither source resolves, the
    consumer fails loudly and reports the reason rather than guessing a region and
    producing an opaque authentication failure."
    """
    conn = _msk_conn(brokers=brokers, aws_region="")
    with pytest.raises(KafkaConfigurationError, match="region"):
        resolve_aws_region(conn)


# ── Re-validation of the stored row ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("conn", "rule"),
    [
        pytest.param(
            KafkaConnection(
                brokers=_MSK_BROKERS,
                security_protocol="SASL_PLAINTEXT",
                sasl_mechanism="AWS_MSK_IAM",
            ),
            "rule 4 — AWS_MSK_IAM requires SASL_SSL",
            id="msk-iam-on-an-unencrypted-wire",
        ),
        pytest.param(
            KafkaConnection(
                brokers="kafka.evil.tld:9098",
                security_protocol="SASL_SSL",
                sasl_mechanism="AWS_MSK_IAM",
            ),
            "rule 6 — every broker host must have the MSK broker shape",
            id="msk-iam-pointed-at-a-foreign-host",
        ),
        pytest.param(
            KafkaConnection(
                brokers="ec2-203-0-113-25.compute-1.amazonaws.com:9098",
                security_protocol="SASL_SSL",
                sasl_mechanism="AWS_MSK_IAM",
            ),
            "rule 6 — an AWS host that is not an MSK broker is still refused",
            id="SECURITY-msk-iam-pointed-at-an-aws-host-that-is-not-a-broker",
        ),
        pytest.param(
            KafkaConnection(
                brokers=_MSK_BROKERS,
                security_protocol="SASL_SSL",
                sasl_mechanism="AWS_MSK_IAM",
                aws_region="eu-west-1",
            ),
            "rule 7 — a stored region contradicting the broker hosts",
            id="SECURITY-msk-iam-region-contradicting-the-hosts",
        ),
        pytest.param(
            KafkaConnection(
                brokers=(
                    "b-1.imazon.abc123.c2.kafka.us-east-1.amazonaws.com:9098,"
                    "b-2.imazon.abc123.c2.kafka.eu-west-1.amazonaws.com:9098"
                ),
                security_protocol="SASL_SSL",
                sasl_mechanism="AWS_MSK_IAM",
            ),
            "rule 7 — a stored mixed-region broker list",
            id="SECURITY-msk-iam-mixed-region-broker-list",
        ),
        pytest.param(
            KafkaConnection(
                brokers="kafka:9093",
                security_protocol="SASL_SSL",
                sasl_mechanism="SCRAM-SHA-512",
                sasl_username="",
            ),
            "rule 2 — a credential mechanism needs a username",
            id="scram-without-a-username",
        ),
        pytest.param(
            KafkaConnection(
                brokers="kafka:9092", security_protocol="PLAINTEXT", sasl_mechanism="PLAIN"
            ),
            "rule 1 — a mechanism is rejected with a non-SASL protocol",
            id="mechanism-under-plaintext",
        ),
    ],
)
def test_stored_row_violating_a_rule_is_refused_rather_than_dialled(
    conn: KafkaConnection, rule: str
) -> None:
    """A rule-violating stored row raises instead of producing a client.

    ``peripheral_config.settings`` is untyped JSONB that direct SQL or dev seeding can
    populate behind the API, so the consumer re-asserts the admin API's rules against
    what it reads. The IAM cases are the load-bearing ones: they are what keeps the
    pod's IAM identity from being pointed somewhere it was never granted for.

    spec: feature/BACKEND.md §Kafka connection — "**The consumer re-validates the
    protocol/mechanism combination when it builds a client**, instead of trusting the
    stored row to satisfy the API's rules … A row that fails re-validation is treated as
    a configuration error and reported through ``peripheral_health`` — the consumer does
    not attempt the connection."
    """
    with patch(
        "src.backend.admin.datahub_secret.get_datahub_kafka_sasl_password",
        side_effect=AssertionError("no credential may be read for an invalid row"),
    ):
        with pytest.raises(KafkaConfigurationError, match="invalid"):
            build_consumer_config(conn)


# ── KafkaFaultState: the sticky latch ────────────────────────────────────────


def test_fault_survives_repeated_reads() -> None:
    """A latched fault is not drained by reading it.

    librdkafka re-emits ``ALL_BROKERS_DOWN`` / ``_AUTHENTICATION`` on a backoff that
    grows to roughly ten seconds, so a read-and-clear latch drained every five seconds
    would land in the gap and report ``ok`` for a consumer that has never authenticated.
    This encodes a fixed flapping bug — the non-flapping property is asserted directly.

    spec: feature/BACKEND.md §Health reporting — the row reports ``error`` "on a
    connection or authentication failure"; the signal exists because the failure is
    "otherwise unobservable from any HTTP surface".
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    state.record("SASL authentication error: Authentication failed")

    for _ in range(10):
        assert state.error == "SASL authentication error: Authentication failed", (
            "a fault must persist across reads until positive evidence of recovery"
        )


def test_fresh_state_reports_neither_fault_nor_evidence() -> None:
    """Before anything happens, nothing is known — which is not the same as healthy.

    spec: feature/BACKEND.md §Health reporting — "``unknown`` covers both 'never
    reported' and 'no consumer deployed'".
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    assert state.error is None
    assert state.connected is False


def test_positive_evidence_clears_a_latched_fault() -> None:
    """Recovery is asserted by evidence — a partition assignment or a committed message.

    spec: feature/BACKEND.md §Health reporting — ``ok`` "once subscribed and polling".
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    state.record("Connection refused")
    assert state.error is not None  # backstop: the fault was latched

    state.record_healthy()
    assert state.error is None
    assert state.connected is True


# ── Log-repeat suppression ───────────────────────────────────────────────────
#
# REGRESSION — a defect found in live cluster operation, not a hypothetical.
# On the dev cluster a single sustained ``_ALL_BROKERS_DOWN`` condition produced
# **1162 identical error lines in 60 seconds**: ``_make_error_cb`` logged on every
# librdkafka callback, and librdkafka re-emits an unchanged fault on its reconnect
# backoff. The health *row* was correctly de-duplicated throughout — only the logging
# path lacked the discipline. In production that buries every other line exactly when
# an operator is reading them to diagnose the outage.
#
# The fix put the "is this new?" judgement on the latch, as the return values of
# ``record`` / ``record_healthy``. Those returns are the seam these tests pin; nothing
# here needs a broker or a configured logger.
#
# Keep these: the behaviour was implemented plausibly and shipped untested, which is
# why nothing objected when it was wrong.

# The measured repeat count from the incident. Used verbatim rather than a token N=2,
# because the property under test is that suppression is unbounded — one line no matter
# how long the outage lasts.
_OBSERVED_FAULT_REPEATS = 1162


def _kafka_error(code: int, message: str) -> KafkaError:
    """A real ``KafkaError`` whose ``str()`` embeds *message*.

    The real type rather than a stub, because ``str(err)`` — not the bare message — is
    both what reaches the log and what the dedupe compares, and librdkafka renders it as
    ``KafkaError{code=…,val=…,str="…"}``. A stub with a tidy ``__str__`` would test a
    string the consumer never actually sees.
    """
    return KafkaError(code, message)


def _all_brokers_down(message: str) -> KafkaError:
    """A latched-fault error — one of the two codes that drive the health row."""
    return _kafka_error(KafkaError._ALL_BROKERS_DOWN, message)  # noqa: SLF001


def _transient(message: str) -> KafkaError:
    """A non-latched transport error — logged, but not reflected in the health row."""
    return _kafka_error(KafkaError._TRANSPORT, message)  # noqa: SLF001


# ── KafkaFaultState return contracts ─────────────────────────────────────────


def test_record_returns_true_for_a_first_fault() -> None:
    """``record`` reports a newly latched fault as new, so the caller logs it once.

    REGRESSION (live cluster): the return value is what suppresses the 1162-line flood.

    spec: feature/BACKEND.md §Health reporting — a connection failure must be visible;
    the first occurrence is the line that carries that signal.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    assert KafkaFaultState().record("All broker connections are down") is True


def test_record_returns_false_for_every_identical_repeat() -> None:
    """An unchanged fault is reported as not-new, however many times it arrives.

    Driven with the **measured** repeat count from the incident (1162 callbacks in 60
    seconds) rather than a token repeat, because the property is that suppression is
    unbounded in the length of the outage.

    REGRESSION (live cluster): 1162 identical faults must yield exactly one "new".

    spec: feature/BACKEND.md §Health reporting — the row reports the last observed state,
    not one entry per callback; src/shared/datahub/consumer.py KafkaFaultState.record.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    message = "Local: All broker connections are down"

    new_verdicts = [state.record(message) for _ in range(_OBSERVED_FAULT_REPEATS)]

    assert new_verdicts[0] is True, "the first occurrence is new"
    assert not any(new_verdicts[1:]), (
        f"only the first of {_OBSERVED_FAULT_REPEATS} identical faults may report as new; "
        f"{sum(new_verdicts)} did"
    )
    assert sum(new_verdicts) == 1


def test_record_returns_true_when_the_fault_message_changes() -> None:
    """A different fault is new again — suppression must not hide a changed diagnosis.

    An outage that turns from "brokers down" into "authentication failed" is a different
    problem, and the line that says so is the one an operator needs.

    spec: feature/BACKEND.md §Health reporting — ``error`` carries "the message"; a
    changed message is a changed state.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    assert state.record("Local: All broker connections are down") is True
    assert state.record("Local: All broker connections are down") is False
    assert state.record("SASL authentication error: Authentication failed") is True, (
        "a changed fault must be reported as new even while a fault is already latched"
    )


def test_record_healthy_returns_the_cleared_fault_after_an_outage() -> None:
    """Recovery from a latched fault hands back the fault it cleared.

    That return is what distinguishes a genuine outage→recovery transition, worth a log
    line, from ordinary steady-state progress.

    spec: feature/BACKEND.md §Health reporting — ``ok`` "once subscribed and polling",
    after an ``error`` state.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    state.record("Local: All broker connections are down")

    assert state.record_healthy() == "Local: All broker connections are down"


def test_record_healthy_returns_none_on_a_fresh_latch() -> None:
    """A first successful connect is not a recovery.

    Returning a value here would log "recovered" on every consumer start, which is both
    untrue and noise on the happy path.

    spec: feature/BACKEND.md §Health reporting — ``unknown`` covers "never reported"; a
    first ``ok`` is a first report, not a transition out of ``error``.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    assert KafkaFaultState().record_healthy() is None


def test_record_healthy_returns_none_on_repeat_calls() -> None:
    """Only the first healthy call after a fault is a recovery.

    ``record_healthy`` fires on every committed message; without this, a busy topic would
    log a recovery per message.

    spec: feature/BACKEND.md §Kafka Consumers — offsets are committed per message, so
    this path runs at message rate.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    state.record("Local: All broker connections are down")

    assert state.record_healthy() is not None  # backstop: the recovery did happen
    assert state.record_healthy() is None
    assert state.record_healthy() is None


def test_repeat_suppression_does_not_perturb_the_latch_state() -> None:
    """The returns are additive: ``error`` and ``connected`` behave exactly as before.

    The health row is derived from these two properties, so a suppression scheme that
    disturbed them would trade a logging bug for a reporting bug. Asserted across the
    full measured repeat count.

    spec: feature/BACKEND.md §Health reporting — the row reports ``error`` on a
    connection failure and ``ok`` on positive evidence; the sticky-latch semantics are
    unchanged by the logging fix.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    state = KafkaFaultState()
    message = "Local: All broker connections are down"

    for _ in range(_OBSERVED_FAULT_REPEATS):
        state.record(message)

    assert state.error == message, (
        "the latch must still hold the fault after suppressed repeats — the health row "
        "reads this, and it must say 'error' for the whole outage"
    )
    assert state.connected is False, "no positive evidence has been seen"

    state.record_healthy()
    assert state.error is None
    assert state.connected is True


# ── _make_error_cb: one line per distinct fault ──────────────────────────────
#
# Log assertions use ``structlog.testing.capture_logs`` rather than pytest's ``caplog``.
# structlog is unconfigured in this project, so it falls back to ``PrintLogger`` writing
# to stdout and never reaches stdlib logging — ``caplog`` captures **zero** records here,
# and a caplog-based assertion would pass without observing anything. Each test below
# asserts a non-empty capture as its backstop, so a future structlog configuration change
# that broke capture fails loudly instead of passing vacuously.


def test_kafka_error_str_is_stable_across_identical_instances() -> None:
    """The precondition the whole suppression rests on: ``str(err)`` is deterministic.

    Dedupe compares ``str(err)`` between callbacks, and librdkafka hands the callback a
    fresh ``KafkaError`` each time. If that rendering carried anything instance-specific
    — an address, a counter, a timestamp — every repeat would compare as new and the
    flood would return with the suppression code still in place and looking correct.

    REGRESSION-adjacent (live cluster): pins the assumption the fix depends on, which no
    other test would catch failing.

    spec: src/shared/datahub/consumer.py KafkaFaultState.record / _make_error_cb — both
    compare on the rendered message.
    """
    first = _all_brokers_down("Local: All broker connections are down")
    second = _all_brokers_down("Local: All broker connections are down")

    assert first is not second, "backstop: these must be genuinely distinct objects"
    assert str(first) == str(second), (
        "two KafkaErrors with the same code and message must render identically, or "
        f"repeat-suppression cannot work: {str(first)!r} != {str(second)!r}"
    )


def _fault_lines(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "kafka_connection_fault"]


def _transient_lines(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "kafka_client_error"]


def test_sustained_outage_logs_exactly_one_fault_line() -> None:
    """The regression itself: 1162 identical callbacks produce one log line.

    REGRESSION (live cluster): this exact count was observed in 60 seconds against a
    single ``_ALL_BROKERS_DOWN`` condition before the fix.

    spec: feature/BACKEND.md §Health reporting — the connection fault must be reported;
    reporting it 1162 times buries the surrounding lines an operator needs during the
    outage that produced them.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb

    state = KafkaFaultState()
    error_cb = _make_error_cb(state)
    err = _all_brokers_down("Local: All broker connections are down")

    with structlog.testing.capture_logs() as logs:
        for _ in range(_OBSERVED_FAULT_REPEATS):
            # A FRESH error object per callback, as librdkafka delivers. Reusing one
            # instance would let an identity-based dedupe pass this test while flooding
            # in production.
            error_cb(_all_brokers_down("Local: All broker connections are down"))

    lines = _fault_lines(logs)
    assert len(lines) == 1, (
        f"{_OBSERVED_FAULT_REPEATS} identical faults must produce exactly one log line; "
        f"got {len(lines)}"
    )
    assert lines[0]["error"] == str(err), "the logged value is str(err), verbatim"
    assert "Local: All broker connections are down" in lines[0]["error"], (
        "the operator-facing description must survive into the line"
    )
    assert lines[0]["log_level"] == "error"

    # The health row still sees the fault for the whole outage.
    assert state.error == str(err)


def test_a_changed_fault_logs_again() -> None:
    """Suppression is per message, not a one-line-per-process cap.

    Without this, the test above would be satisfied by an implementation that logs the
    first fault and then goes silent forever — including through a changed diagnosis.

    spec: feature/BACKEND.md §Health reporting — ``error`` carries the current message.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb

    error_cb = _make_error_cb(KafkaFaultState())
    down = _all_brokers_down("Local: All broker connections are down")
    auth = _all_brokers_down("SASL authentication error: Authentication failed")

    with structlog.testing.capture_logs() as logs:
        for _ in range(50):
            error_cb(_all_brokers_down("Local: All broker connections are down"))
        for _ in range(50):
            error_cb(_all_brokers_down("SASL authentication error: Authentication failed"))

    lines = _fault_lines(logs)
    assert [line["error"] for line in lines] == [str(down), str(auth)], (
        f"expected one line per distinct fault, in order; got {[x['error'] for x in lines]}"
    )


def test_recovery_from_an_outage_emits_a_recovered_line() -> None:
    """A genuine outage→recovery transition is logged once, naming the prior fault.

    spec: feature/BACKEND.md §Health reporting — ``ok`` "once subscribed and polling",
    following an ``error``; the operator needs the transition, not just the new state.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb, _note_healthy

    state = KafkaFaultState()
    error_cb = _make_error_cb(state)
    err = _all_brokers_down("Local: All broker connections are down")

    with structlog.testing.capture_logs() as logs:
        for _ in range(_OBSERVED_FAULT_REPEATS):
            error_cb(_all_brokers_down("Local: All broker connections are down"))
        _note_healthy(state)

    recovered = [e for e in logs if e.get("event") == "kafka_connection_recovered"]
    assert len(recovered) == 1, f"expected exactly one recovery line; got {len(recovered)}"
    assert recovered[0]["previous_error"] == str(err), (
        "the recovery line must name the fault it cleared, so the transition is readable"
    )
    assert recovered[0]["log_level"] == "info", "a recovery is good news, not a warning"


def test_a_first_connect_and_steady_state_emit_no_recovery_line() -> None:
    """No recovery line without a fault to recover from.

    ``_note_healthy`` runs on partition assignment and on every committed message, so a
    version that logged unconditionally would emit a line per message on the happy path —
    the same flood in a different place.

    spec: feature/BACKEND.md §Kafka Consumers — offsets are committed per message.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _note_healthy

    state = KafkaFaultState()

    with structlog.testing.capture_logs() as logs:
        _note_healthy(state)  # partition assignment — a first connect, not a recovery
        for _ in range(500):
            _note_healthy(state)  # a committed message each

    assert [e for e in logs if e.get("event") == "kafka_connection_recovered"] == []
    # Backstop: the healthy evidence was still recorded, so the calls did run.
    assert state.connected is True


def test_transient_errors_are_deduped_too() -> None:
    """The non-latched branch got the same discipline, deduped on its own state.

    librdkafka re-emits per-broker transport failures on the same backoff, so leaving
    this branch undeduped would reproduce the flood at ``warning`` level.

    spec: feature/BACKEND.md §Kafka Consumers — deserialization/transport problems are
    logged and skipped; src/shared/datahub/consumer.py _make_error_cb.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb

    error_cb = _make_error_cb(KafkaFaultState())

    with structlog.testing.capture_logs() as logs:
        for _ in range(_OBSERVED_FAULT_REPEATS):
            error_cb(_transient("Receive failed: Disconnected"))

    lines = _transient_lines(logs)
    assert len(lines) == 1, (
        f"identical transient errors must collapse to one line; got {len(lines)}"
    )
    assert "Receive failed: Disconnected" in lines[0]["error"]
    assert lines[0]["log_level"] == "warning", (
        "a transient error is not a latched fault, so it stays a warning"
    )


def test_a_changed_transient_error_logs_again() -> None:
    """The backstop for the test above: transient dedupe is per message, not a cap.

    spec: as above.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb

    error_cb = _make_error_cb(KafkaFaultState())
    disconnected = _transient("Receive failed: Disconnected")
    timed_out = _transient("Connection setup timed out")

    with structlog.testing.capture_logs() as logs:
        for _ in range(20):
            error_cb(_transient("Receive failed: Disconnected"))
        for _ in range(20):
            error_cb(_transient("Connection setup timed out"))

    assert [line["error"] for line in _transient_lines(logs)] == [
        str(disconnected),
        str(timed_out),
    ]


def test_the_two_dedupe_channels_do_not_suppress_each_other() -> None:
    """A latched fault and a transient error are tracked separately.

    A shared "last message seen" would let one kind of problem silence the first
    occurrence of the other — the one occurrence that always has to be logged.

    spec: feature/BACKEND.md §Health reporting — only the latched fault codes drive the
    health row; the transient branch is a separate signal.
    """
    import structlog

    from src.shared.datahub.consumer import KafkaFaultState, _make_error_cb

    same_text = "Connection refused"
    error_cb = _make_error_cb(KafkaFaultState())

    with structlog.testing.capture_logs() as logs:
        error_cb(_transient(same_text))
        error_cb(_all_brokers_down(same_text))

    assert len(_transient_lines(logs)) == 1, "the transient error must be logged"
    assert len(_fault_lines(logs)) == 1, (
        "a latched fault carrying the same text as a preceding transient error must "
        "still log — the two channels are independent"
    )


# ── Inner-loop health flush ──────────────────────────────────────────────────


def _consumer_that_polls_nothing() -> MagicMock:
    consumer = MagicMock()
    consumer.poll = MagicMock(return_value=None)
    return consumer


async def _run_one_reconfig_tick(faults) -> MagicMock:
    """Drive ``_run_inner_loop`` through exactly one reconfig check; return the reporter."""
    from src.shared.datahub.consumer import _run_inner_loop

    current = KafkaConnection(brokers="kafka:9092", security_protocol="PLAINTEXT")
    changed = KafkaConnection(brokers="kafka-new:9092", security_protocol="PLAINTEXT")
    health = MagicMock()
    health.report = AsyncMock()

    with patch(
        "src.shared.datahub.consumer.read_kafka_connection",
        AsyncMock(return_value=changed),
    ):
        await _run_inner_loop(_consumer_that_polls_nothing(), MagicMock(), current, faults, health)

    return health


@pytest.mark.asyncio
async def test_inner_loop_reports_error_when_a_fault_is_latched() -> None:
    """A latched fault is flushed to ``peripheral_health`` as ``error`` with its message.

    spec: feature/BACKEND.md §Health reporting — "``error`` with the message on a
    connection or authentication failure".
    """
    from src.shared.datahub.consumer import KafkaFaultState

    faults = KafkaFaultState()
    faults.record("SASL authentication error")

    health = await _run_one_reconfig_tick(faults)
    health.report.assert_awaited_once_with("error", "SASL authentication error")


@pytest.mark.asyncio
async def test_inner_loop_reports_error_even_when_evidence_preceded_the_fault() -> None:
    """A fault outranks earlier evidence — health does not flap back to ``ok``.

    A consumer that connected and then lost the brokers must read ``error``, not ``ok``.

    spec: feature/BACKEND.md §Health reporting — ``error`` on a connection failure.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    faults = KafkaFaultState()
    faults.record_healthy()
    faults.record("All broker connections are down")

    health = await _run_one_reconfig_tick(faults)
    health.report.assert_awaited_once_with("error", "All broker connections are down")


@pytest.mark.asyncio
async def test_inner_loop_reports_ok_only_on_positive_evidence() -> None:
    """``ok`` requires a partition assignment or a committed message.

    spec: feature/BACKEND.md §Health reporting — "``ok`` once subscribed and polling".
    """
    from src.shared.datahub.consumer import KafkaFaultState

    faults = KafkaFaultState()
    faults.record_healthy()

    health = await _run_one_reconfig_tick(faults)
    health.report.assert_awaited_once_with("ok")


@pytest.mark.asyncio
async def test_inner_loop_leaves_the_row_alone_with_neither_fault_nor_evidence() -> None:
    """Nothing known yet → no write, so the row stays ``unknown``.

    Asserting health on the strength of "no error seen yet" is exactly the flapping this
    design avoids: a consumer that has not authenticated emits no message and may not
    have re-emitted its fault within the check interval.

    spec: feature/BACKEND.md §Health reporting — ``unknown`` covers "never reported";
    ``ok`` is written "once subscribed and polling", which neither holds here.
    """
    from src.shared.datahub.consumer import KafkaFaultState

    health = await _run_one_reconfig_tick(KafkaFaultState())
    health.report.assert_not_awaited()


# ── HealthReporter ───────────────────────────────────────────────────────────


def _patch_health_write() -> tuple:
    """Patch the lazily imported health-write surface; return (ctxs, calls list)."""
    calls: list[tuple] = []

    async def _report(db, name, status, error):
        calls.append((name, status, error))

    ctxs = (
        patch(
            "src.backend.admin.peripheral_health.report_peripheral_health",
            side_effect=_report,
        ),
        patch(
            "src.shared.db.session.SessionLocal",
            return_value=_async_ctx(),
        ),
    )
    return ctxs, calls


def _async_ctx() -> AsyncMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_health_reporter_writes_the_datahub_row() -> None:
    """The reporter upserts the ``datahub`` peripheral_health row.

    spec: feature/BACKEND.md §Health reporting — "The consumer therefore upserts the
    ``datahub`` row of ``peripheral_health``".
    """
    from src.shared.datahub.consumer import HealthReporter

    (write, session), calls = _patch_health_write()
    reporter = HealthReporter()
    with write, session:
        await reporter.report("error", "boom")

    assert calls == [("datahub", "error", "boom")]


@pytest.mark.asyncio
async def test_health_reporter_suppresses_an_unchanged_repeat() -> None:
    """An unchanged status is not rewritten on every reconfig tick.

    spec: feature/BACKEND.md §Health reporting — the row holds the *last observed* state;
    the write is a state-change signal, not a per-poll log.
    """
    from src.shared.datahub.consumer import HealthReporter

    (write, session), calls = _patch_health_write()
    reporter = HealthReporter()
    with write, session:
        await reporter.report("ok")
        await reporter.report("ok")

    assert calls == [("datahub", "ok", None)], f"expected one write, got {calls!r}"


@pytest.mark.asyncio
async def test_health_reporter_writes_again_when_the_state_changes() -> None:
    """A transition is always written, however soon after the previous report.

    spec: feature/BACKEND.md §Health reporting — the row reports ``ok``/``error`` as the
    connection state changes.
    """
    from src.shared.datahub.consumer import HealthReporter

    (write, session), calls = _patch_health_write()
    reporter = HealthReporter()
    with write, session:
        await reporter.report("ok")
        await reporter.report("error", "brokers down")
        await reporter.report("ok")

    assert calls == [
        ("datahub", "ok", None),
        ("datahub", "error", "brokers down"),
        ("datahub", "ok", None),
    ]


@pytest.mark.asyncio
async def test_health_reporter_scrubs_the_live_credential_from_the_message() -> None:
    """A SASL password on the wire never reaches the row an Admin reads back.

    ``str(err)`` from librdkafka is persisted verbatim, so the guarantee is made at the
    reporter rather than inherited from the client library.

    spec: spec/API.md §DataHub Kafka security — ``kafka_sasl_password`` is write-only and
    "The plaintext values are never returned"; feature/BACKEND.md §Health reporting —
    the failure message is surfaced through ``GET /admin/peripherals/datahub``.
    """
    from src.shared.datahub.consumer import HealthReporter

    (write, session), calls = _patch_health_write()
    reporter = HealthReporter()
    reporter.set_redaction("sup3r-s3cret-pw")
    with write, session:
        await reporter.report("error", "auth failed for user dataspoke/sup3r-s3cret-pw")

    assert len(calls) == 1
    _, _, message = calls[0]
    assert "sup3r-s3cret-pw" not in message, (
        f"the live SASL password must not be persisted in peripheral_health; got {message!r}"
    )
    # Backstop: the rest of the message survives, so scrubbing did not blank the report.
    assert "auth failed for user dataspoke" in message


@pytest.mark.asyncio
async def test_health_reporting_failure_does_not_stop_the_consumer() -> None:
    """A failed health write is swallowed — observability never halts consumption.

    spec: feature/BACKEND.md §Best-Effort Operations — non-critical operations execute
    best-effort; §Health reporting — the row is a report of the connection, not the
    connection itself.
    """
    from src.shared.datahub.consumer import HealthReporter

    with (
        patch(
            "src.backend.admin.peripheral_health.report_peripheral_health",
            side_effect=RuntimeError("db down"),
        ),
        patch("src.shared.db.session.SessionLocal", return_value=_async_ctx()),
    ):
        await HealthReporter().report("ok")  # must not raise
