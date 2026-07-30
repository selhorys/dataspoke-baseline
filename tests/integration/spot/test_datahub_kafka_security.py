"""Spot integration tests for the DataHub Kafka security surface.

Three sections, each one concern per test:

1. **A ``peripheral_health`` row.**  Nothing on the admin surface writes it — the event
   consumer does, and the dev cluster runs none (``event-consumer.enabled`` is off by
   default).  The row is therefore seeded by direct SQL and read back through
   ``GET /api/v1/admin/peripherals/datahub``.

2. **A rule-violating ``peripheral_config`` row.**  The admin API rejects every such
   body with ``422``, so the only way to obtain one is to write it behind the API —
   which is precisely the threat the consumer's re-validation exists for.
   ``peripheral_config.settings`` is untyped JSONB that direct SQL or dev seeding can
   populate without passing through the request schema.

3. **The REST round-trip of the Kafka tuple** — masking, the credential never reaching
   the DB row, the rotation counter, ``is_configured`` independence, the seven rules'
   ``422`` (all seven rules), and the stored-password clearing on ``AWS_MSK_IAM``.  This lives
   at spot rather than api-wired because ``spec/TESTING.md`` §Api-Wired Integration Tests
   reserves that directory for the five ``USE_CASE_en.md`` user stories, and the DataHub
   Kafka admin surface belongs to none of them; §Spot integration tests → Boundary
   explicitly permits a spot test that "call[s] the API over HTTP".

Every test that writes the ``datahub`` peripheral snapshots and restores it, and a
module-scoped fixture returns the Kafka tuple to the dev cluster's unsecured baseline, so
the live DataHub wiring the other integration modules depend on survives a run.

Spec traceability:
- spec/feature/BACKEND_SCHEMA.md §peripheral_health — the five-column upserted row;
  "Absence of a row and ``status='unknown'`` mean the same thing to readers".
- spec/API.md §DataHub Kafka security — "The consumer writes its outcome to the
  ``peripheral_health`` row keyed ``datahub`` … and this route reads it back";
  "``status`` is ``unknown`` when the consumer has never reported".
- spec/feature/BACKEND.md §Kafka connection — "**The consumer re-validates the
  protocol/mechanism combination when it builds a client**, instead of trusting the
  stored row to satisfy the API's rules … A row that fails re-validation is treated as a
  configuration error and reported through ``peripheral_health`` — the consumer does not
  attempt the connection."
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from src.backend.admin.peripheral_service import (
    DatahubConfigDTO,
    get_peripheral_config,
    invalidate_peripheral_config_cache,
)
from src.shared.datahub.consumer import KafkaConnection, build_consumer_config
from src.shared.exceptions import KafkaConfigurationError

_ADMIN_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"


@pytest_asyncio.fixture
async def datahub_settings_restored(async_session) -> AsyncIterator[dict]:
    """Snapshot ``peripheral_config.datahub`` settings and restore them afterwards.

    The dev cluster's live DataHub wiring lives in this row; a test that writes a
    deliberately invalid tuple into it must put the real one back.
    """
    result = await async_session.execute(
        text("SELECT settings FROM dataspoke.peripheral_config WHERE name = 'datahub'")
    )
    row = result.scalar_one_or_none()
    original = dict(row) if row else None

    yield original or {}

    if original is None:
        await async_session.execute(
            text("DELETE FROM dataspoke.peripheral_config WHERE name = 'datahub'")
        )
    else:
        await async_session.execute(
            text(
                "UPDATE dataspoke.peripheral_config SET settings = CAST(:s AS jsonb) "
                "WHERE name = 'datahub'"
            ),
            {"s": json.dumps(original)},
        )
    await async_session.commit()
    invalidate_peripheral_config_cache("datahub")

    # Restore assertion: the live wiring is back exactly as it was.
    check = await async_session.execute(
        text("SELECT settings FROM dataspoke.peripheral_config WHERE name = 'datahub'")
    )
    restored = check.scalar_one_or_none()
    assert (dict(restored) if restored else None) == original, (
        "the dev cluster's datahub peripheral settings must be restored verbatim"
    )


@pytest_asyncio.fixture
async def datahub_health_removed(async_session) -> AsyncIterator[None]:
    """Ensure no ``datahub`` peripheral_health row exists before and after the test."""
    await async_session.execute(
        text("DELETE FROM dataspoke.peripheral_health WHERE name = 'datahub'")
    )
    await async_session.commit()
    yield
    await async_session.execute(
        text("DELETE FROM dataspoke.peripheral_health WHERE name = 'datahub'")
    )
    await async_session.commit()


# ── 1. peripheral_health seeded by SQL, read back through the API ────────────


@pytest.mark.asyncio
async def test_absent_health_row_reads_back_as_unknown(
    api_client, admin_headers, datahub_health_removed
) -> None:
    """With no row, ``GET /admin/peripherals/datahub`` reports ``health.status = unknown``.

    This is the state of every deployment that runs no event consumer — which is the dev
    cluster's default.

    spec: API.md §DataHub Kafka security — "``status`` is ``unknown`` when the consumer
    has never reported — including every deployment that runs no consumer at all."
    """
    resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
    assert resp.status_code == 200, resp.text

    health = resp.json()["health"]
    assert health["status"] == "unknown"
    assert health["last_error"] is None
    assert health["last_ok_at"] is None


@pytest.mark.asyncio
async def test_seeded_error_health_row_surfaces_on_the_admin_route(
    api_client, admin_headers, async_session, datahub_health_removed
) -> None:
    """A consumer-written failure row is returned verbatim by the admin route.

    ``is_configured`` cannot express "the values are present but the connection does not
    work"; this field is what does.

    spec: API.md §DataHub Kafka security — "The consumer writes its outcome to the
    ``peripheral_health`` row keyed ``datahub`` … and this route reads it back";
    BACKEND_SCHEMA.md §peripheral_health — ``last_error`` holds the most recent failure
    message, ``last_ok_at`` the last successful connection.
    """
    await async_session.execute(
        text(
            # Upsert, not a bare insert: the row is cluster-wide singleton state and the
            # running event consumer rewrites it on its own schedule, so it can reappear
            # between the removal fixture and this seed. What the test asserts is that the
            # admin route returns whatever the row holds — so the seed must win
            # deterministically rather than race the consumer for who inserts first.
            "INSERT INTO dataspoke.peripheral_health "
            "(name, status, last_error, last_ok_at, updated_at) VALUES "
            "('datahub', 'error', :err, NOW() - INTERVAL '3 hours', NOW()) "
            "ON CONFLICT (name) DO UPDATE SET "
            "status = EXCLUDED.status, last_error = EXCLUDED.last_error, "
            "last_ok_at = EXCLUDED.last_ok_at, updated_at = EXCLUDED.updated_at"
        ),
        {"err": "SASL authentication error: Authentication failed"},
    )
    await async_session.commit()

    resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
    assert resp.status_code == 200, resp.text

    health = resp.json()["health"]
    assert health["status"] == "error"
    assert health["last_error"] == "SASL authentication error: Authentication failed"
    assert health["last_ok_at"] is not None, (
        "an error report leaves the previous last_ok_at intact so a reader can see how "
        "long the outage has lasted"
    )
    assert health["updated_at"] is not None


@pytest.mark.asyncio
async def test_seeded_ok_health_row_surfaces_on_the_admin_route(
    api_client, admin_headers, async_session, datahub_health_removed
) -> None:
    """A healthy report reads back as ``ok`` with no failure message.

    spec: feature/BACKEND.md §Health reporting — "``ok`` once subscribed and polling";
    BACKEND_SCHEMA.md §peripheral_health — ``last_error`` is ``NULL`` when never failed.
    """
    await async_session.execute(
        text(
            # Upsert for the same reason as the error-row seed above.
            "INSERT INTO dataspoke.peripheral_health "
            "(name, status, last_error, last_ok_at, updated_at) VALUES "
            "('datahub', 'ok', NULL, NOW(), NOW()) "
            "ON CONFLICT (name) DO UPDATE SET "
            "status = EXCLUDED.status, last_error = EXCLUDED.last_error, "
            "last_ok_at = EXCLUDED.last_ok_at, updated_at = EXCLUDED.updated_at"
        )
    )
    await async_session.commit()

    health = (await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)).json()["health"]
    assert health["status"] == "ok"
    assert health["last_error"] is None
    assert health["last_ok_at"] is not None


# ── 2. The consumer refuses a rule-violating stored row ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "rule"),
    [
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_PLAINTEXT",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
            },
            "rule 4 — AWS_MSK_IAM on an unencrypted wire",
            id="rule4-msk-iam-over-sasl-plaintext",
        ),
        pytest.param(
            {
                "kafka_brokers": "kafka.attacker-controlled.tld:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
            },
            "rule 6 — the pod's IAM identity redirected to a foreign host",
            id="rule6-msk-iam-pointed-at-a-foreign-host",
        ),
        pytest.param(
            {
                "kafka_brokers": "ec2-203-0-113-25.compute-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
            },
            "rule 6 — a genuine AWS host that is not an MSK broker",
            id="SECURITY-rule6-aws-host-that-is-not-an-msk-broker",
        ),
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_aws_region": "eu-west-1",
            },
            "rule 7 — a stored region contradicting the broker hosts",
            id="SECURITY-rule7-region-contradicting-the-hosts",
        ),
        pytest.param(
            {
                "kafka_brokers": "kafka:9093",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "SCRAM-SHA-512",
                "kafka_sasl_username": "",
            },
            "rule 2 — a credential mechanism with no username",
            id="rule2-scram-without-a-username",
        ),
    ],
)
async def test_consumer_refuses_a_rule_violating_stored_row(
    async_session, datahub_settings_restored, settings: dict, rule: str
) -> None:
    """A row written behind the API is re-validated on read and refused, not dialled.

    The row is written with raw SQL precisely because the admin API would reject the same
    tuple with ``422`` — bypassing it is the scenario the re-check defends against. The
    two IAM cases are the load-bearing ones: without the re-check, a row written by
    direct SQL could put a SigV4 token minted from the consumer pod's identity on an
    unencrypted wire, or hand it to a host the operator does not own.

    spec: feature/BACKEND.md §Kafka connection — "``peripheral_config`` is a plain table
    that direct SQL or dev seeding can write behind the API … A row that fails
    re-validation is treated as a configuration error and reported through
    ``peripheral_health`` — the consumer does not attempt the connection."
    """
    merged = {**datahub_settings_restored, **settings}
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.peripheral_config (name, settings) "
            "VALUES ('datahub', CAST(:s AS jsonb)) "
            "ON CONFLICT (name) DO UPDATE SET settings = CAST(:s AS jsonb)"
        ),
        {"s": json.dumps(merged)},
    )
    await async_session.commit()
    invalidate_peripheral_config_cache("datahub")

    dto = await get_peripheral_config(async_session, "datahub")
    assert isinstance(dto, DatahubConfigDTO), "the seeded row must resolve to a DataHub DTO"

    conn = KafkaConnection(
        brokers=dto.kafka_brokers,
        security_protocol=dto.kafka_security_protocol or "PLAINTEXT",
        sasl_mechanism=dto.kafka_sasl_mechanism,
        sasl_username=dto.kafka_sasl_username,
        aws_region=dto.kafka_aws_region,
        sasl_password_version=dto.kafka_sasl_password_version,
    )

    with pytest.raises(KafkaConfigurationError) as exc_info:
        build_consumer_config(conn)

    assert "invalid" in str(exc_info.value).lower(), (
        f"{rule}: the refusal must name the stored settings as invalid; "
        f"got {exc_info.value!s}"
    )


@pytest.mark.asyncio
async def test_consumer_accepts_a_valid_stored_row(
    async_session, datahub_settings_restored
) -> None:
    """The backstop: a compliant stored row does produce a client config.

    Without this, the refusals above could pass for a build step that always raises.

    spec: feature/BACKEND.md §Kafka connection — the client-property mapping;
    spec/API.md §DataHub Kafka security — ``PLAINTEXT`` is a valid stored tuple.
    """
    merged = {
        **datahub_settings_restored,
        "kafka_brokers": "kafka:9092",
        "kafka_security_protocol": "PLAINTEXT",
        "kafka_sasl_mechanism": "",
        "kafka_sasl_username": "",
        "kafka_aws_region": "",
    }
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.peripheral_config (name, settings) "
            "VALUES ('datahub', CAST(:s AS jsonb)) "
            "ON CONFLICT (name) DO UPDATE SET settings = CAST(:s AS jsonb)"
        ),
        {"s": json.dumps(merged)},
    )
    await async_session.commit()
    invalidate_peripheral_config_cache("datahub")

    dto = await get_peripheral_config(async_session, "datahub")
    assert isinstance(dto, DatahubConfigDTO)

    config = build_consumer_config(
        KafkaConnection(brokers=dto.kafka_brokers, security_protocol="PLAINTEXT")
    )
    assert config["bootstrap.servers"] == "kafka:9092"
    assert "security.protocol" not in config, (
        "a PLAINTEXT connection carries no security properties at all"
    )


# ── 3. REST round-trip of the Kafka tuple ────────────────────────────────────
#
# Placed at spot rather than api_wired: spec/TESTING.md §Api-Wired Integration Tests
# reserves that directory for "one of the five ``USE_CASE_en.md`` user stories", and the
# DataHub Kafka admin surface is not part of any of them. Spot explicitly permits a test
# that "call[s] the API over HTTP" (§Spot integration tests → Boundary).


@pytest.fixture(scope="module", autouse=True)
def _restore_dev_kafka_baseline():
    """Return the dev DataHub peripheral's Kafka tuple to its unsecured baseline.

    The tests below write a SASL configuration and a Kafka credential into the live
    peripheral. The dev cluster's DataHub reaches Kafka unsecured, so the tuple is reset
    to ``PLAINTEXT`` with no mechanism, no username, no region, and no stored password.
    ``token`` and ``gms_url`` are never touched by these tests, so the REST wiring that
    every other module depends on is unaffected.
    """
    import os

    import httpx

    yield

    domain = os.environ.get("DATASPOKE_KUBE_INGRESS_DOMAIN")
    internal_token = os.environ.get("DATASPOKE_TEST_INTERNAL_TOKEN", "")
    brokers = os.environ.get("DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS", "")
    if not (domain and internal_token):
        return

    with httpx.Client(timeout=15.0) as client:
        resp = client.patch(
            f"http://api.{domain}/internal/admin/peripherals/datahub",
            headers={"X-Internal-Token": internal_token, "Content-Type": "application/json"},
            json={
                "kafka_brokers": brokers,
                "kafka_security_protocol": "PLAINTEXT",
                "kafka_sasl_mechanism": "",
                "kafka_sasl_username": "",
                "kafka_aws_region": "",
                "kafka_sasl_password": "",
            },
        )
    # Restore assertion: the baseline is actually back, not merely attempted.
    assert resp.status_code == 200, f"Kafka baseline restore failed: {resp.text}"
    restored = resp.json()
    assert restored["kafka_security_protocol"] == "PLAINTEXT"
    assert restored["kafka_sasl_mechanism"] == ""
    assert restored["kafka_sasl_password"] == ""


@pytest.mark.asyncio
async def test_kafka_tuple_round_trips_with_the_password_masked(
    api_client, admin_headers, async_session
) -> None:
    """PATCH stores the tuple; GET returns it with the password masked as "********".

    The credential is write-only: the plaintext must not appear anywhere in the response.

    spec: API.md §DataHub Kafka security — the field table; ``kafka_sasl_password`` is
    "Write-only, same ``""`` unset / ``"********"`` set convention as ``token``".
    """
    patch_resp = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={
            "kafka_brokers": "kafka.dataspoke.svc.cluster.local:9093",
            "kafka_security_protocol": "SASL_SSL",
            "kafka_sasl_mechanism": "SCRAM-SHA-512",
            "kafka_sasl_username": "dataspoke-consumer",
            "kafka_sasl_password": "spot-rotation-secret-1",
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert "spot-rotation-secret-1" not in patch_resp.text, (
        "the plaintext credential must never appear in a PATCH response"
    )

    get_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()

    assert body["kafka_brokers"] == "kafka.dataspoke.svc.cluster.local:9093"
    assert body["kafka_security_protocol"] == "SASL_SSL"
    assert body["kafka_sasl_mechanism"] == "SCRAM-SHA-512"
    assert body["kafka_sasl_username"] == "dataspoke-consumer"
    assert body["kafka_sasl_password"] == "********"
    assert "spot-rotation-secret-1" not in get_resp.text


@pytest.mark.asyncio
async def test_kafka_password_never_lands_in_peripheral_config_settings(
    api_client, admin_headers, async_session
) -> None:
    """The credential is routed to the K8s Secret; the DB row never holds it.

    Read straight out of the JSONB column, because that is the artifact the guarantee is
    about — a masked HTTP response would look identical either way.

    spec: API.md §DataHub Kafka security — "Routed to ``dataspoke-datahub-secret`` key
    ``kafka_sasl_password``, never the DB"; BACKEND_SCHEMA.md §peripheral_config —
    "Secret fields (DataHub ``token`` and ``kafka_sasl_password`` …) are never stored
    here".
    """
    resp = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={
            "kafka_brokers": "kafka.dataspoke.svc.cluster.local:9093",
            "kafka_security_protocol": "SASL_SSL",
            "kafka_sasl_mechanism": "SCRAM-SHA-512",
            "kafka_sasl_username": "dataspoke-consumer",
            "kafka_sasl_password": "spot-plaintext-must-not-persist",
        },
    )
    assert resp.status_code == 200, resp.text

    row = await async_session.execute(
        text("SELECT settings FROM dataspoke.peripheral_config WHERE name = 'datahub'")
    )
    settings = dict(row.scalar_one())

    assert "kafka_sasl_password" not in settings, (
        f"the credential key must not exist in the DB row; settings keys were "
        f"{sorted(settings)}"
    )
    assert "spot-plaintext-must-not-persist" not in json.dumps(settings), (
        "the plaintext must not appear anywhere in peripheral_config.settings"
    )
    # Backstop: the non-secret fields of the same PATCH did land, so the write happened.
    assert settings["kafka_sasl_username"] == "dataspoke-consumer"
    assert settings["kafka_security_protocol"] == "SASL_SSL"


@pytest.mark.asyncio
async def test_kafka_password_write_bumps_the_version_counter(
    api_client, admin_headers
) -> None:
    """Each password write advances ``kafka_sasl_password_version``.

    The counter is the only DB-plane evidence of a Secret-only rotation, so a running
    consumer detects the rotation from the row alone.

    spec: API.md §DataHub Kafka security — ``kafka_sasl_password_version`` "Incremented
    by ``PATCH`` whenever the password Secret is written, so a long-running consumer sees
    a rotation as a DB-plane change".
    """
    before = (await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)).json()[
        "kafka_sasl_password_version"
    ]

    rotated = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={"kafka_sasl_password": "spot-rotation-secret-2"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["kafka_sasl_password_version"] == before + 1

    # A PATCH that writes no credential must leave the counter alone, or the consumer
    # would rebuild its client on every unrelated edit.
    unrelated = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={"kafka_sasl_username": "dataspoke-consumer"},
    )
    assert unrelated.status_code == 200, unrelated.text
    assert unrelated.json()["kafka_sasl_password_version"] == before + 1


@pytest.mark.asyncio
async def test_is_configured_is_unaffected_by_the_kafka_password(
    api_client, admin_headers
) -> None:
    """Setting and clearing the Kafka credential never moves ``is_configured``.

    ``is_configured`` keys on the GMS ``token`` alone; a DataHub peripheral without a
    Kafka credential is fully configured for every REST-based flow.

    spec: API.md §DataHub Kafka security — the Kafka fields "do not participate in
    ``is_configured``"; §Admin — "For DataHub the participating secret is ``token``
    alone — ``kafka_sasl_password`` is optional and never affects the flag."
    """
    baseline = (await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)).json()[
        "is_configured"
    ]

    with_password = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={
            "kafka_security_protocol": "SASL_SSL",
            "kafka_sasl_mechanism": "SCRAM-SHA-512",
            "kafka_sasl_username": "dataspoke-consumer",
            "kafka_sasl_password": "spot-rotation-secret-3",
        },
    )
    assert with_password.status_code == 200, with_password.text
    assert with_password.json()["kafka_sasl_password"] == "********"
    assert with_password.json()["is_configured"] is baseline

    cleared = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={"kafka_sasl_password": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["kafka_sasl_password"] == "", (
        "an explicit empty string clears the credential"
    )
    assert cleared.json()["is_configured"] is baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field", "rule"),
    [
        pytest.param(
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": ""},
            "kafka_sasl_mechanism",
            "rule 1 — a mechanism is required with a SASL protocol",
            id="rule1-sasl-protocol-without-mechanism",
        ),
        pytest.param(
            {"kafka_security_protocol": "PLAINTEXT", "kafka_sasl_mechanism": "PLAIN"},
            "kafka_sasl_mechanism",
            "rule 1 — a mechanism is rejected with a non-SASL protocol",
            id="rule1-mechanism-under-plaintext",
        ),
        pytest.param(
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "SCRAM-SHA-256",
                "kafka_sasl_username": "",
            },
            "kafka_sasl_username",
            "rule 2 — a credential mechanism needs a username",
            id="rule2-scram-without-username",
        ),
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "svc",
            },
            "kafka_sasl_username",
            "rule 3 — a username is rejected under AWS_MSK_IAM",
            id="rule3-username-under-msk-iam",
        ),
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
                "kafka_sasl_password": "typed-credential",
            },
            "kafka_sasl_password",
            "rule 3 — a submitted password is rejected under AWS_MSK_IAM",
            id="rule3-password-under-msk-iam",
        ),
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_PLAINTEXT",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
            },
            "kafka_security_protocol",
            "rule 4 — AWS_MSK_IAM requires SASL_SSL; no silent upgrade",
            id="rule4-msk-iam-over-sasl-plaintext",
        ),
        pytest.param(
            {
                "kafka_security_protocol": "PLAINTEXT",
                "kafka_sasl_mechanism": "",
                "kafka_aws_region": "us-east-1",
            },
            "kafka_aws_region",
            "rule 5 — a region is accepted only with AWS_MSK_IAM",
            id="rule5-region-without-msk-iam",
        ),
        pytest.param(
            {
                "kafka_brokers": "kafka.attacker-controlled.tld:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
            },
            "kafka_brokers",
            "rule 6 — every broker host must have the MSK broker shape under AWS_MSK_IAM",
            id="rule6-msk-iam-pointed-at-a-foreign-host",
        ),
        pytest.param(
            {
                "kafka_brokers": "ec2-203-0-113-25.compute-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
            },
            "kafka_brokers",
            "rule 6 — an AWS host that is not an MSK broker is still refused",
            id="SECURITY-rule6-aws-host-that-is-not-an-msk-broker",
        ),
        pytest.param(
            {
                "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
                "kafka_aws_region": "eu-west-1",
            },
            "kafka_aws_region",
            "rule 7 — an explicit region contradicting the broker hosts",
            id="SECURITY-rule7-region-contradicting-the-hosts",
        ),
        pytest.param(
            {
                "kafka_brokers": (
                    "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098,"
                    "b-2.imazon.abc.c2.kafka.eu-west-1.amazonaws.com:9098"
                ),
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_username": "",
            },
            "kafka_brokers",
            "rule 7 — a mixed-region broker list",
            id="SECURITY-rule7-mixed-region-broker-list",
        ),
    ],
)
async def test_rule_violation_returns_422_invalid_parameter(
    api_client, admin_headers, body: dict, field: str, rule: str
) -> None:
    """Every rule violation is ``422 INVALID_PARAMETER`` naming the offending field.

    Covers all seven rules, including the two rejections rule 7 adds and the
    AWS-host-that-is-not-a-broker case rule 6 turns on the MSK broker shape.

    The Kafka tuple registers no error code of its own.

    spec: API.md §DataHub Kafka security — "**Validation is normative and every violation
    is ``422 INVALID_PARAMETER``** — the existing generic code, with the offending field
    named in ``detail``. The Kafka tuple registers no error code of its own."
    """
    resp = await api_client.patch(_ADMIN_PERIPHERALS_DH, headers=admin_headers, json=body)

    assert resp.status_code == 422, f"{rule}: got {resp.status_code} {resp.text}"
    payload = resp.json()
    assert payload["error_code"] == "INVALID_PARAMETER", payload
    assert payload["detail"]["field"] == field, (
        f"{rule}: the offending field must be named in detail; got {payload.get('detail')}"
    )


@pytest.mark.asyncio
async def test_switching_to_msk_iam_clears_the_stored_password(
    api_client, admin_headers
) -> None:
    """A stored credential is cleared when the effective mechanism becomes AWS_MSK_IAM.

    The mechanism authenticates with the pod's IAM identity and never reads a password;
    leaving one behind would keep a live credential in the Secret that nothing uses and
    that GET would keep reporting as ``"********"``.

    spec: API.md §DataHub Kafka security — "The **stored password is handled differently:
    it is cleared** whenever the effective mechanism becomes ``AWS_MSK_IAM``, and ``GET``
    reports ``kafka_sasl_password: ""`` from then on."
    """
    # Arrange: a working SCRAM configuration with a stored credential.
    scram = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={
            "kafka_brokers": "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098",
            "kafka_security_protocol": "SASL_SSL",
            "kafka_sasl_mechanism": "SCRAM-SHA-512",
            "kafka_sasl_username": "dataspoke-consumer",
            "kafka_sasl_password": "spot-credential-to-be-dropped",
        },
    )
    assert scram.status_code == 200, scram.text
    assert scram.json()["kafka_sasl_password"] == "********", (
        "backstop: the credential must actually be stored before the switch"
    )

    # Act: switch to AWS_MSK_IAM, clearing the username in the same request as the spec
    # requires — the username is rejected, the password is cleared.
    switched = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=admin_headers,
        json={"kafka_sasl_mechanism": "AWS_MSK_IAM", "kafka_sasl_username": ""},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["kafka_sasl_password"] == ""

    # Assert: GET reports it cleared from then on.
    after = (await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)).json()
    assert after["kafka_sasl_mechanism"] == "AWS_MSK_IAM"
    assert after["kafka_sasl_password"] == ""
    assert after["kafka_security_protocol"] == "SASL_SSL", (
        "the stored protocol is always the protocol the consumer uses"
    )
