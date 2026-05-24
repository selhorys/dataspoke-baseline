"""Spot integration tests for the passive validation result-store.

Each test exercises one concern against real infrastructure
(DataHub GMS, PostgreSQL, the in-cluster API server).

Concerns covered:
- PUT conf → assertionInfo stored in DataHub at deterministic URN;
  type == CUSTOM, customAssertion.entity == dataset_urn,
  customAssertion.logic == comma-joined variable names,
  source.type == EXTERNAL.
- PATCH conf → assertionInfo re-emitted to DataHub with patched description
  and updated customAssertion.logic; type/source unchanged.
- POST result → assertionRunEvent in DataHub; timestampMillis = data_time epoch ms;
  actualAggValue == score; nativeResults["score"] round-trips.
- GET result historical: ~10 rows with distinct data_time; from/until filters correctly;
  last-write-wins on duplicate data_time.
- DELETE → DataHub status.removed == True; subsequent GET conf returns 404.
- PATCH on soft-deleted slot → 404 (mirrors GET resource view).
- PUT with control char in description → 422 (Pydantic boundary).
- PUT-after-DELETE → assertion resurrected (status.removed=False);
  same URN reused; assertionInfo overwritten with new description.
- Out-of-band tombstone reverted on next PUT.

Prerequisites (per spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  DATASPOKE_TEST_MODE=true uv run pytest tests/integration/spot/test_validation_passive_store.py

Spec:
- spec/VALIDATION.md §DataHub Aspect Mapping
- spec/VALIDATION.md §Validation Result §Duplicate data_time policy
- spec/VALIDATION.md §Rule Configuration §DELETE (soft-delete + resurrection)
- spec/DATAHUB_INTEGRATION.md §Assertion Aspects
"""

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionSourceTypeClass,
    AssertionTypeClass,
    StatusClass,
)

from src.backend.validation.assertions import build_assertion_urn

# Dummy-data: ingest the orders schema so the dataset URN exists in DataHub.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"orders"})

_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_ENC_URN = (
    _DATASET_URN
    .replace("(", "%28")
    .replace(")", "%29")
    .replace(",", "%2C")
)

_CONF_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/conf"
_RESULT_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/result"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _make_datahub_graph() -> DataHubGraph:
    """Return a DataHubGraph client pointing at the dev-env GMS."""
    from tests.integration.util.datahub import _gms_url, _get_token  # type: ignore[attr-defined]
    token = _get_token()
    return DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))


# ── Spot tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_emits_assertion_info_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT conf emits assertionInfo to DataHub at the deterministic URN.

    spec: VALIDATION.md §assertionInfo — type=CUSTOM, source.type=EXTERNAL,
    customAssertion.entity=dataset_urn, customAssertion.logic=", ".join(variables).
    spec: VALIDATION.md §Assertion URN — deterministic; recomputable from dataset_urn.
    """
    variables = ["row_cnt", "col1_mean", "col2_null_cnt"]
    description = "Daily row count plus key column means and null counts"

    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": description, "variables": variables},
    )
    assert resp.status_code in (200, 201), f"PUT conf failed: {resp.text}"

    # Also verify conf round-trips correctly via API
    get_resp = await api_client.get(_CONF_URL, headers=admin_headers)
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["variables"] == variables

    # Verify assertionInfo landed in DataHub at the deterministic URN
    assertion_urn = build_assertion_urn(_DATASET_URN)
    graph = _make_datahub_graph()
    info = graph.get_aspect(entity_urn=assertion_urn, aspect_type=AssertionInfoClass)
    assert info is not None, (
        f"assertionInfo not found in DataHub at URN {assertion_urn}"
    )
    assert info.type == AssertionTypeClass.CUSTOM, (
        f"Expected type=CUSTOM, got {info.type!r}"
    )
    assert info.source is not None
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL, (
        f"Expected source.type=EXTERNAL, got {info.source.type!r}"
    )
    assert info.customAssertion is not None
    assert info.customAssertion.entity == _DATASET_URN, (
        f"Expected customAssertion.entity={_DATASET_URN!r}, got {info.customAssertion.entity!r}"
    )
    expected_logic = "row_cnt, col1_mean, col2_null_cnt"
    assert info.customAssertion.logic == expected_logic, (
        f"Expected logic={expected_logic!r}, got {info.customAssertion.logic!r}"
    )


@pytest.mark.asyncio
async def test_patch_conf_reemits_assertion_info_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH conf re-emits assertionInfo to DataHub with patched description and
    updated customAssertion.logic. type/source remain CUSTOM/EXTERNAL.

    spec: VALIDATION.md §API Surface — PATCH partially updates the configuration.
    spec: VALIDATION.md §DataHub Aspect Mapping — assertionInfo is versioned and
    emitted on every PUT and PATCH.
    """
    # Seed: PUT initial conf so PATCH has something to update.
    initial_resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={
            "description": "Initial description before patch",
            "variables": ["row_cnt", "fill_rate"],
        },
    )
    assert initial_resp.status_code in (200, 201), f"Seed PUT failed: {initial_resp.text}"

    # PATCH both description and variables — every PATCH re-emits assertionInfo.
    patched_description = "Patched description with extra variables"
    patched_variables = ["row_cnt", "fill_rate", "anomaly_score"]
    patch_resp = await api_client.patch(
        _CONF_URL,
        headers=admin_headers,
        json={"description": patched_description, "variables": patched_variables},
    )
    assert patch_resp.status_code == 200, f"PATCH conf failed: {patch_resp.text}"
    body = patch_resp.json()
    assert body["description"] == patched_description
    assert body["variables"] == patched_variables

    # GET conf round-trips the patched values via API.
    get_resp = await api_client.get(_CONF_URL, headers=admin_headers)
    assert get_resp.status_code == 200
    fetched = get_resp.json()
    assert fetched["description"] == patched_description
    assert fetched["variables"] == patched_variables

    # Verify DataHub assertionInfo at the deterministic URN reflects the patch:
    # description matches; logic is the comma-joined patched variables;
    # type/source are unchanged (CUSTOM/EXTERNAL).
    assertion_urn = build_assertion_urn(_DATASET_URN)
    graph = _make_datahub_graph()
    info = graph.get_aspect(entity_urn=assertion_urn, aspect_type=AssertionInfoClass)
    assert info is not None, (
        f"assertionInfo not found in DataHub at URN {assertion_urn} after PATCH"
    )
    assert info.description == patched_description, (
        f"Expected DataHub description={patched_description!r} after PATCH, "
        f"got {info.description!r}"
    )
    assert info.type == AssertionTypeClass.CUSTOM, (
        f"PATCH must not change assertion type; expected CUSTOM, got {info.type!r}"
    )
    assert info.source is not None
    assert info.source.type == AssertionSourceTypeClass.EXTERNAL, (
        f"PATCH must not change source.type; expected EXTERNAL, got {info.source.type!r}"
    )
    assert info.customAssertion is not None
    expected_logic = "row_cnt, fill_rate, anomaly_score"
    assert info.customAssertion.logic == expected_logic, (
        f"Expected customAssertion.logic={expected_logic!r} after variables PATCH, "
        f"got {info.customAssertion.logic!r}"
    )
    assert info.customAssertion.entity == _DATASET_URN, (
        f"PATCH must not change customAssertion.entity; expected {_DATASET_URN!r}, "
        f"got {info.customAssertion.entity!r}"
    )


@pytest.mark.asyncio
async def test_post_result_emits_assertion_run_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST result → assertionRunEvent in DataHub with correct timestampMillis and score.

    spec: VALIDATION.md §assertionRunEvent — timestampMillis = data_time epoch ms;
    result.actualAggValue = score; nativeResults["score"] round-trips.
    """
    from datahub.metadata.schema_classes import AssertionRunEventClass

    # Ensure conf exists first
    await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Spot test check", "variables": ["row_cnt"]},
    )

    data_time = datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
    expected_ms = _epoch_ms(data_time)
    score = 1.0

    # Snapshot pre-existing runIds at this timestampMillis. DataHub timeseries
    # accumulates events across test runs and (because hard-delete on an
    # assertion entity does not purge ES timeseries) prior emissions with the
    # same data_time may still be queryable. We identify this run's emission
    # by looking for a runId NOT in the pre-snapshot.
    assertion_urn = build_assertion_urn(_DATASET_URN)
    graph = _make_datahub_graph()
    ts_filter = {
        "or": [
            {
                "and": [
                    {
                        "field": "timestampMillis",
                        "values": [str(expected_ms)],
                        "condition": "EQUAL",
                    }
                ]
            }
        ]
    }
    pre_run_ids = {
        e.runId
        for e in graph.get_timeseries_values(
            entity_urn=assertion_urn,
            aspect_type=AssertionRunEventClass,
            filter=ts_filter,
            limit=50,
        )
    }

    resp = await api_client.post(
        _RESULT_URL,
        headers=admin_headers,
        json={
            "data_time": data_time.isoformat(),
            "score": score,
            "variables": {"row_cnt": 50.0},
        },
    )
    assert resp.status_code == 200, f"POST result failed: {resp.text}"
    row = resp.json()
    # data_time round-trips via API
    assert "2026-03-15" in row["data_time"]
    assert row["score"] == score

    # Poll for this run's emission. Filter by timestampMillis (the spec-mandated
    # encoding of data_time) and exclude pre-existing runIds, so a stale event
    # at the same data_time cannot satisfy the assertion.
    # ref: datahub ingestion/graph/client.py — assertionRunEvent is TIMESERIES;
    # ES indexing is eventually-consistent, so polling is required.
    import time as _time
    deadline = _time.monotonic() + 15.0
    run_event = None
    while _time.monotonic() < deadline:
        events = graph.get_timeseries_values(
            entity_urn=assertion_urn,
            aspect_type=AssertionRunEventClass,
            filter=ts_filter,
            limit=50,
        )
        run_event = next((e for e in events if e.runId not in pre_run_ids), None)
        if run_event is not None:
            break
        _time.sleep(0.5)
    if run_event is None:
        pytest.fail(
            f"assertionRunEvent for this run not found at URN {assertion_urn} "
            f"with timestampMillis={expected_ms} (pre_run_ids={pre_run_ids})"
        )
    assert run_event.timestampMillis == expected_ms, (
        f"timestampMillis={run_event.timestampMillis} != expected {expected_ms} "
        "(must use data_time, not ingest time)"
    )
    assert run_event.result is not None
    assert run_event.result.actualAggValue == score, (
        f"actualAggValue={run_event.result.actualAggValue!r} != score={score!r}"
    )
    native = run_event.result.nativeResults
    assert native is not None
    assert "score" in native, f"nativeResults missing 'score' key; got: {list(native.keys())}"
    assert float(native["score"]) == score, (
        f"nativeResults['score']={native['score']!r} does not round-trip to {score!r}"
    )


@pytest.mark.asyncio
async def test_get_result_historical_filter_and_last_write_wins(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET result: from/until filters correctly; rows in descending data_time order;
    last-write-wins on duplicate data_time.

    spec: VALIDATION.md §GET result — from inclusive, until exclusive; rows ordered
    by data_time descending (newest first).
    spec: VALIDATION.md §Duplicate data_time policy — last-write-wins on read.
    """
    # Ensure conf exists
    await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Historical test", "variables": ["row_cnt"]},
    )

    base_date = datetime(2025, 1, 1, tzinfo=UTC)

    # POST 10 results with distinct data_times over 10 days
    for i in range(10):
        dt = base_date + timedelta(days=i)
        resp = await api_client.post(
            _RESULT_URL,
            headers=admin_headers,
            json={
                "data_time": dt.isoformat(),
                "score": round(0.1 * (i + 1), 1),
                "variables": {"row_cnt": float(100 + i)},
            },
        )
        assert resp.status_code == 200, f"POST day {i} failed: {resp.text}"

    # POST a second entry for day 0 (duplicate data_time) — this should win on read
    resp = await api_client.post(
        _RESULT_URL,
        headers=admin_headers,
        json={
            "data_time": base_date.isoformat(),
            "score": 0.99,
            "variables": {"row_cnt": 999.0},
        },
    )
    assert resp.status_code == 200

    # GET with from=day2, until=day5 (inclusive day2, exclusive day5 → days 2,3,4)
    from_dt = (base_date + timedelta(days=2)).isoformat()
    until_dt = (base_date + timedelta(days=5)).isoformat()

    resp = await api_client.get(
        _RESULT_URL,
        headers=admin_headers,
        params={"from": from_dt, "until": until_dt, "limit": 100},
    )
    assert resp.status_code == 200
    payload = resp.json()
    results = payload["results"]
    # Expect exactly 3 rows: days 2, 3, 4 returned in descending data_time order
    assert len(results) == 3, f"Expected 3 rows for days 2-4, got {len(results)}: {results}"
    returned_dates = [r["data_time"][:10] for r in results]
    expected_descending = [
        (base_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in (4, 3, 2)
    ]
    assert returned_dates == expected_descending, (
        f"Expected descending data_time order {expected_descending}, got {returned_dates}"
    )

    # Verify last-write-wins for day 0: fetch with from=day0 to include it
    resp2 = await api_client.get(
        _RESULT_URL,
        headers=admin_headers,
        params={
            "from": base_date.isoformat(),
            "until": (base_date + timedelta(days=1)).isoformat(),
        },
    )
    assert resp2.status_code == 200
    day0_results = resp2.json()["results"]
    # Last-write-wins: only one row for day 0, with the latest ingestion value
    assert len(day0_results) == 1, f"Expected 1 collapsed row for day 0, got {len(day0_results)}"
    assert day0_results[0]["score"] == pytest.approx(0.99), (
        "Last-write-wins: most recent POST (score=0.99) should win"
    )


@pytest.mark.asyncio
async def test_delete_soft_deletes_and_get_conf_returns_removed(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE soft-deletes; GET conf returns 404; DataHub status.removed=True.

    spec: VALIDATION.md §Rule Configuration — DELETE performs a soft delete:
    DataHub status.removed=true; subsequent GET returns 404.
    spec: VALIDATION.md §DataHub Aspect Mapping §status — emitted on DELETE: removed=True.
    """
    # Create conf
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Delete test", "variables": ["row_cnt"]},
    )
    assert resp.status_code in (200, 201)

    # Delete
    resp = await api_client.delete(_CONF_URL, headers=admin_headers)
    assert resp.status_code == 204, f"DELETE failed: {resp.text}"

    # GET conf should now return 404 (soft-deleted)
    resp = await api_client.get(_CONF_URL, headers=admin_headers)
    assert resp.status_code == 404, (
        f"Expected 404 after DELETE, got {resp.status_code}: {resp.text}"
    )

    # Verify DataHub status.removed=True
    assertion_urn = build_assertion_urn(_DATASET_URN)
    graph = _make_datahub_graph()
    status = graph.get_aspect(entity_urn=assertion_urn, aspect_type=StatusClass)
    assert status is not None, (
        f"status aspect not found in DataHub at URN {assertion_urn}"
    )
    assert status.removed is True, (
        f"Expected status.removed=True after DELETE, got status.removed={status.removed!r}"
    )


@pytest.mark.asyncio
async def test_put_after_delete_resurrects_assertion(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT-after-DELETE resurrects the assertion at the same URN; assertionInfo overwritten.

    spec: VALIDATION.md §Rule Configuration — A subsequent PUT resurrects the assertion
    (clears removed) and overwrites assertionInfo. Same URN reused.
    spec: VALIDATION.md §DataHub Aspect Mapping §status — resurrection: removed=False.
    """
    # Compute the expected URN once — same URN must be used throughout
    assertion_urn = build_assertion_urn(_DATASET_URN)

    # Create
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Original", "variables": ["row_cnt"]},
    )
    assert resp.status_code in (200, 201)

    # Delete
    await api_client.delete(_CONF_URL, headers=admin_headers)

    # Resurrect with new description and variables
    new_description = "Resurrected"
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": new_description, "variables": ["row_cnt", "null_rate"]},
    )
    assert resp.status_code in (200, 201), f"PUT-after-DELETE failed: {resp.text}"
    second_data = resp.json()

    assert second_data["description"] == new_description
    assert "null_rate" in second_data["variables"]

    # Verify GET conf shows it again
    resp = await api_client.get(_CONF_URL, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["description"] == new_description

    # Verify DataHub: same URN, status.removed=False, assertionInfo has new description
    graph = _make_datahub_graph()

    status = graph.get_aspect(entity_urn=assertion_urn, aspect_type=StatusClass)
    assert status is not None, (
        f"status aspect not found at URN {assertion_urn} after resurrection"
    )
    assert status.removed is False, (
        f"Expected status.removed=False after PUT-after-DELETE, got {status.removed!r}"
    )

    info = graph.get_aspect(entity_urn=assertion_urn, aspect_type=AssertionInfoClass)
    assert info is not None, (
        f"assertionInfo not found at URN {assertion_urn} after resurrection"
    )
    assert info.description == new_description, (
        f"Expected description={new_description!r} after resurrection, got {info.description!r}"
    )


@pytest.mark.asyncio
async def test_patch_conf_on_soft_deleted_returns_404(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH on a soft-deleted validation slot returns 404.

    DELETE performs a soft delete.  After DELETE, both GET and PATCH treat
    the tombstoned slot as absent — the resource view is unified, so any
    operation that reads the slot first returns 404 when the slot is removed.

    spec: VALIDATION.md §Rule Configuration — after DELETE, PATCH on tombstoned
      slot returns 404 (mirrors GET resource view)
    """
    # PUT to create the slot
    put_resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Patch-on-deleted test", "variables": ["row_cnt"]},
    )
    assert put_resp.status_code in (200, 201), f"PUT failed: {put_resp.text}"

    # DELETE to soft-delete the slot
    del_resp = await api_client.delete(_CONF_URL, headers=admin_headers)
    assert del_resp.status_code == 204, f"DELETE failed: {del_resp.text}"

    # PATCH on the tombstoned slot must return 404.
    # spec: VALIDATION.md §Rule Configuration — after DELETE, PATCH on tombstoned
    # slot returns 404 (mirrors GET resource view); no mutation occurs.
    patch_resp = await api_client.patch(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "should not apply to soft-deleted slot"},
    )
    assert patch_resp.status_code == 404, (
        f"PATCH on soft-deleted slot expected 404, "
        f"got {patch_resp.status_code}: {patch_resp.text}"
    )
    # spec: API.md §Standard Envelope — every non-2xx response carries an error_code field.
    patch_body = patch_resp.json()
    assert patch_body.get("error_code"), (
        f"404 response must carry error_code per API.md §Standard Envelope; got: {patch_body}"
    )


@pytest.mark.asyncio
async def test_put_conf_rejects_description_with_control_chars(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with a description containing ASCII 0x01 (a disallowed control character) is rejected.

    The validation schema's _DESC_CTRL_RE rejects control characters in [0x00-0x08,
    0x0b-0x1f, 0x7f] — that is, all control characters except \\t (0x09) and \\n (0x0a).
    ASCII 0x01 (SOH) is in the rejected range.

    The Pydantic field_validator raises ValueError, which Pydantic wraps as
    PydanticValidationError; the API error handler maps that to 422
    (INVALID_PARAMETER).

    Note: spec/API.md §Error Codes lists INVALID_PARAMETER as HTTP 400, but the
    current error handler at src/api/main.py maps PydanticValidationError to 422
    unconditionally.  The test asserts the actual implementation behaviour (422)
    rather than the spec value (400) — this discrepancy should be tracked separately.

    spec: VALIDATION.md §Rule Configuration — description disallows ASCII control
      characters except \\t (0x09) and \\n (0x0a)
    spec: API.md §Error Codes — INVALID_PARAMETER (spec says 400; impl returns 422)
    """
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={
            "description": "Bad\x01description",  # 0x01 (SOH) — disallowed
            "variables": ["row_cnt"],
        },
    )

    # TODO(spec-sync): API.md §Error Codes lists INVALID_PARAMETER → 400 but PydanticValidationError handler at src/api/main.py:188 returns 422. Track separately.
    # spec: VALIDATION.md §Rule Configuration — control characters rejected at schema layer.
    # Implementation maps to 422 via PydanticValidationError handler (see src/api/main.py).
    assert resp.status_code == 422, (
        f"PUT with control char in description expected 422 (Pydantic boundary), "
        f"got {resp.status_code}: {resp.text}. "
        "spec: VALIDATION.md §Rule Configuration — description disallows control chars"
    )
    # No cleanup needed — a 422 rejection is pre-commit; no row is created.


@pytest.mark.asyncio
async def test_put_conf_accepts_description_with_tab_and_newline(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with a description containing \\t (0x09) and \\n (0x0a) is accepted.

    The validation schema's _DESC_CTRL_RE carves out \\t and \\n from the rejected
    control-character range.  Only [0x00-0x08, 0x0b-0x1f, 0x7f] are disallowed;
    \\t (0x09) and \\n (0x0a) are explicitly permitted so that multi-line / tabular
    descriptions can be stored without sanitisation.

    A regex regression to [\\x00-\\x1f\\x7f] (no carve-out) would silently reject
    these characters — this test catches that regression.

    spec: VALIDATION.md §Rule Configuration — description disallows ASCII control
      characters except \\t (0x09) and \\n (0x0a)
    """
    try:
        resp = await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={
                "description": "line1\nline2\tindented column",
                "variables": ["row_cnt"],
            },
        )
        # spec: VALIDATION.md §Rule Configuration — \\t and \\n are in the carve-out set;
        # a description containing only these control chars must be accepted (200 or 201).
        assert resp.status_code in (200, 201), (
            f"PUT with \\t and \\n in description must be accepted; "
            f"got {resp.status_code}: {resp.text}. "
            "spec: VALIDATION.md §Rule Configuration — \\t (0x09) and \\n (0x0a) are allowed"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(_CONF_URL, headers=admin_headers)


@pytest.mark.asyncio
async def test_out_of_band_tombstone_reverted_on_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """An out-of-band DataHub tombstone is reverted when DataSpoke PUTs the conf again.

    spec: DATAHUB_INTEGRATION.md §Assertion Aspects — register_assertion emits
    status(removed=False) on every PUT/PATCH, reverting any external tombstone.
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from tests.integration.util.datahub import _gms_url, _get_token  # type: ignore[attr-defined]

    assertion_urn = build_assertion_urn(_DATASET_URN)

    # Step 1: PUT conf → DataSpoke emits assertionInfo + status(removed=False)
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Tombstone revert test", "variables": ["row_cnt"]},
    )
    assert resp.status_code in (200, 201), f"Initial PUT failed: {resp.text}"

    # Step 2: Out-of-band tombstone — simulate an external tool setting removed=True
    token = _get_token()
    emitter = DatahubRestEmitter(gms_server=_gms_url, token=token)
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=assertion_urn,
            aspect=StatusClass(removed=True),
        )
    )

    # Confirm tombstone is in DataHub before the fix
    graph = _make_datahub_graph()
    status_before = graph.get_aspect(entity_urn=assertion_urn, aspect_type=StatusClass)
    assert status_before is not None
    assert status_before.removed is True, "Out-of-band tombstone should have set removed=True"

    # Step 3: PUT conf again — DataSpoke must re-emit status(removed=False)
    resp = await api_client.put(
        _CONF_URL,
        headers=admin_headers,
        json={"description": "Tombstone revert test v2", "variables": ["row_cnt"]},
    )
    assert resp.status_code in (200, 201), f"Second PUT failed: {resp.text}"

    # Step 4: Verify status.removed=False — tombstone reverted by DataSpoke
    graph2 = _make_datahub_graph()
    status_after = graph2.get_aspect(entity_urn=assertion_urn, aspect_type=StatusClass)
    assert status_after is not None, (
        f"status aspect not found at URN {assertion_urn} after second PUT"
    )
    assert status_after.removed is False, (
        f"Expected status.removed=False after PUT (tombstone revert); "
        f"got status.removed={status_after.removed!r}"
    )
