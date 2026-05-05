"""API-wired integration tests — validation spec-hardening invariants.

NOTE: These tests are WRITTEN but NOT RUN per task directive.
Run group: DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/

Concerns covered (derived from spec invariants, NOT implementation behavior):
61. PUT /attr/validation/conf registers assertions in DataHub.
62. PUT returns 502 when DataHub is down — generic message, no URL leak.
63. PATCH /attr/validation/conf with rules registers assertions.
64. PATCH without rules does NOT register assertions.
65. POST /method/validation/run: emit failure → errored=1 (not failed/passed), ERROR result,
    validation_results row has non-empty issues list.
66. 422 on invalid last_modified_field at API layer (before service call).
67. 422 on source field set on non-freshness/volume rule type.

Spec sources:
- spec/DATAHUB_INTEGRATION.md §Assertion Aspects conventions 1-7
- spec/feature/BACKEND.md §Validation Service
- spec/API.md — route contracts
"""

import urllib.parse
from unittest.mock import AsyncMock, patch

import httpx
import pytest

# Imazon Datahub-seeded dataset (catalog schema)
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# Unknown URN for negative-path tests
_UNKNOWN_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.schema.table,DEV)"
_ENCODED_UNKNOWN_URN = urllib.parse.quote(_UNKNOWN_URN, safe="")

# Per-module dummy-data seed
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_BASE_CONF = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
_BASE_RUN = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"


# ── 61. PUT registers assertions ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_validation_conf_registers_assertions_in_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """DATAHUB_INTEGRATION.md convention 6: PUT /attr/validation/conf must register assertions in DataHub.

    The assertion URNs for all submitted rules must exist in DataHub after a 200/201 response.
    """
    from src.backend.validation.assertions import build_assertion_urn

    rules = [
        {"rule_id": "aw-put-r1", "type": "freshness", "lookback_interval": "24h"},
        {"rule_id": "aw-put-r2", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
    ]

    resp = await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": rules,
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )
    assert resp.status_code in (200, 201), f"PUT failed: {resp.text}"

    # Verify assertion URNs exist in DataHub
    for rule in rules:
        urn = build_assertion_urn(_TEST_URN, rule["rule_id"])
        fetched = await datahub_client.get_assertion_info(urn)
        assert fetched is not None, (
            f"Assertion {urn} must exist in DataHub after PUT /attr/validation/conf"
        )

    # Cleanup
    await api_client.delete(_BASE_CONF, headers=admin_headers)


# ── 62. PUT returns 502 when DataHub is down ──────────────────────────────────

@pytest.mark.asyncio
async def test_put_validation_conf_returns_502_when_datahub_down(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DATAHUB_INTEGRATION.md convention 6: DataHub unavailable during registration → 502 with generic message.

    The 502 body must NOT contain the GMS URL or any internal endpoint path.
    error_code must be DATAHUB_UNAVAILABLE. message must be a non-empty generic string.
    """
    from src.shared.exceptions import DataHubUnavailableError

    # Patch DataHubClient.emit_assertion to simulate DataHub unavailability
    with patch(
        "src.shared.datahub.client.DataHubClient.emit_assertion",
        new=AsyncMock(
            side_effect=DataHubUnavailableError(
                "http://datahub-gms:8080/aspects?action=ingestProposal failed"
            )
        ),
    ):
        resp = await api_client.put(
            _BASE_CONF,
            headers=admin_headers,
            json={
                "rules": [{"rule_id": "aw-502-r1", "type": "freshness", "lookback_interval": "24h"}],
                "schedule_tier": "daily",
                "is_enabled": True,
                "owner": "de@imazon.com",
            },
        )

    assert resp.status_code == 502, f"Expected 502 but got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body.get("error_code") == "DATAHUB_UNAVAILABLE"
    # Spec: generic non-empty message (exact wording is impl/copy choice)
    message = body.get("message")
    assert isinstance(message, str) and message  # non-empty

    # Security: must NOT leak internal URL or GMS hostname
    body_text = resp.text
    assert "datahub-gms" not in body_text, "502 body must not leak GMS hostname"
    assert "ingestProposal" not in body_text, "502 body must not contain internal endpoint path"


# ── 63. PATCH with rules registers assertions ─────────────────────────────────

@pytest.mark.asyncio
async def test_patch_validation_conf_with_rules_registers_assertions(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """BACKEND.md §Validation Service: PATCH with rules triggers register_assertion for each rule."""
    from src.backend.validation.assertions import build_assertion_urn

    # Create initial config
    await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": [{"rule_id": "aw-patch-base-r1", "type": "freshness", "lookback_interval": "24h"}],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )

    # PATCH with new rules
    new_rules = [
        {"rule_id": "aw-patch-new-r1", "type": "volume", "condition": {"type": "greater_than", "value": 0}},
    ]
    patch_resp = await api_client.patch(
        _BASE_CONF,
        headers=admin_headers,
        json={"rules": new_rules},
    )
    assert patch_resp.status_code == 200, f"PATCH failed: {patch_resp.text}"

    # Verify new rule's assertion URN exists in DataHub
    urn = build_assertion_urn(_TEST_URN, "aw-patch-new-r1")
    fetched = await datahub_client.get_assertion_info(urn)
    assert fetched is not None, "PATCH with rules must register new rule assertion in DataHub"

    # Cleanup
    await api_client.delete(_BASE_CONF, headers=admin_headers)


# ── 64. PATCH without rules does NOT register ─────────────────────────────────

@pytest.mark.asyncio
async def test_patch_validation_conf_without_rules_does_not_call_register(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """BACKEND.md §Validation Service: PATCH without rules must not call register_assertion."""
    # Create initial config
    await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": [{"rule_id": "aw-noreg-r1", "type": "freshness", "lookback_interval": "24h"}],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )

    emit_calls = []

    async def capture_emit(urn, aspect):
        emit_calls.append(urn)

    # Patch only is_enabled — no rules key
    with patch(
        "src.shared.datahub.client.DataHubClient.emit_assertion",
        new=AsyncMock(side_effect=capture_emit),
    ):
        patch_resp = await api_client.patch(
            _BASE_CONF,
            headers=admin_headers,
            json={"is_enabled": False},
        )

    assert patch_resp.status_code == 200, f"PATCH failed: {patch_resp.text}"
    assert len(emit_calls) == 0, (
        "PATCH without rules must NOT call emit_assertion (register_assertion must be skipped)"
    )

    # Cleanup
    await api_client.delete(_BASE_CONF, headers=admin_headers)


# ── 65. POST run: emit fail → errored, ERROR result, non-empty issues ────────

@pytest.mark.asyncio
async def test_post_run_emit_fail_produces_error_result_and_errored_counter(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """DATAHUB_INTEGRATION.md convention 7: emit failure → errored=1, ERROR assertion_result, non-empty issues.

    The validation_results row for the failed-emit rule must have:
    - assertion_result == "ERROR"
    - issues list is non-empty
    The summary must have errored=1, passed=0, failed=0 for that rule.
    """
    # Setup: config with one rule
    await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": [{"rule_id": "aw-emitfail-r1", "type": "freshness", "lookback_interval": "24h"}],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )

    # Force report_result to return False (emit failure simulation)
    with patch(
        "src.backend.validation.service.report_result",
        new=AsyncMock(return_value=False),
    ):
        run_resp = await api_client.post(
            _BASE_RUN,
            headers=admin_headers,
            json={"dry_run": False},
        )

    assert run_resp.status_code == 200, f"POST /run failed: {run_resp.text}"
    body = run_resp.json()

    # Summary must show errored=1, passed=0
    assert body.get("errored") == 1, (
        "Convention 7: emit failure must increment errored counter (not passed or failed)"
    )
    assert body.get("passed") == 0
    assert body.get("failed") == 0

    # Verify the persisted row in DB using the async_session fixture from root conftest
    from sqlalchemy import select

    from src.shared.db.models import ValidationResult

    result = await async_session.execute(
        select(ValidationResult).where(
            ValidationResult.dataset_urn == _TEST_URN,
            ValidationResult.rule_id == "aw-emitfail-r1",
        )
    )
    rows = result.scalars().all()
    assert len(rows) > 0, "ValidationResult row must exist in DB"
    latest_row = max(rows, key=lambda r: r.measured_at)
    # Spec invariants: assertion_result is ERROR and issues list is non-empty
    assert latest_row.assertion_result == "ERROR", (
        "Convention 7: persisted assertion_result must be ERROR on emit failure"
    )
    assert len(latest_row.issues or []) > 0, (
        "Persisted issues must be non-empty on emit failure"
    )

    # Cleanup
    await api_client.delete(_BASE_CONF, headers=admin_headers)


# ── 66. 422 on invalid last_modified_field ────────────────────────────────────

@pytest.mark.asyncio
async def test_put_returns_422_for_invalid_last_modified_field(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """API schema: invalid last_modified_field with SQL metacharacters → 422 before any service call.

    spec/feature/BACKEND.md §Source discriminator: last_modified_field must match
    \\A[A-Za-z_][A-Za-z0-9_]{0,62}\\Z at the API layer.
    """
    # Inline payload for readability — per feedback_test_readability.md
    resp = await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": [{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                "last_modified_field": "updated_at; DROP TABLE orders",
            }],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )

    # Must be rejected at API layer (422) before reaching the service
    assert resp.status_code == 422, (
        f"Expected 422 for SQL-injection in last_modified_field, got {resp.status_code}: {resp.text}"
    )


# ── 67. 422 on source set on non-freshness/volume rule ────────────────────────

@pytest.mark.asyncio
async def test_put_returns_422_for_source_on_field_rule(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """API schema: source field is reserved for freshness/volume rules only.

    field rule with source: query → 422 before any service call.
    spec/feature/BACKEND.md §Source discriminator: source is only valid for freshness and volume.
    """
    # Inline payload for readability — per feedback_test_readability.md
    resp = await api_client.put(
        _BASE_CONF,
        headers=admin_headers,
        json={
            "rules": [{
                "rule_id": "r1",
                "type": "field",
                "field": "rating_score",
                "source": "query",
                "condition": {"type": "less_than_or_equal_to", "value": 0},
            }],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "de@imazon.com",
        },
    )

    # Must be rejected at API layer (422)
    assert resp.status_code == 422, (
        f"Expected 422 for source on field rule, got {resp.status_code}: {resp.text}"
    )
