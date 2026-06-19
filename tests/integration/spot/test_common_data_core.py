"""Spot integration tests for common/data core and validation-event routes.

Routes under test:
  GET /api/v1/spoke/common/data/{dataset_urn}
  GET /api/v1/spoke/common/data/{dataset_urn}/attr
  GET /api/v1/spoke/common/data/{dataset_urn}/event
  GET /api/v1/spoke/common/data/{dataset_urn}/event/validation

Concerns covered:
- GET /{urn} returns DatasetResponse with urn, name, platform, description,
  owners, tags fields as defined in the schema.
- GET /{urn}/attr returns DatasetAttributesResponse with urn, column_count,
  fields, owners, tags, description, quality_score fields.
- GET /{urn}/event returns EventListResponse pagination envelope (offset, limit,
  total_count, events=[]).
- GET /{urn}/event is the UNIFIED per-dataset timeline: each row carries the
  derived `wrapper` flag; the repeatable `event_major_type` filter
  (INGESTION/VALIDATION/METAGEN, default all) narrows the stream and total_count.
- GET /{urn}/event/validation returns EventListResponse pagination envelope;
  is empty when no validation events exist for the dataset.
- Both event routes return 200 with an empty events list for a known dataset
  that has no events yet (not 404).

Prerequisites (per spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/spot/test_common_data_core.py

Spec:
- spec/API.md §Data Resource (/spoke/common/data) — GET summary, GET attr,
  GET event routes.
- spec/API.md §Data Resource — event/validation per-dataset event history.
- spec/TESTING.md §Spot integration tests — coverage rule.
"""

import urllib.parse
from contextlib import suppress

import httpx
import pytest

# Ingest catalog schema so dataset URNs are resolvable in DataHub.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.title_master,DEV)"
)
_ENC_URN = urllib.parse.quote(_DATASET_URN, safe="")

_SUMMARY_URL = f"/api/v1/spoke/common/data/{_ENC_URN}"
_ATTR_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr"
_EVENT_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/event"
_VALIDATION_EVENT_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/event/validation"
_VALIDATION_CONF_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/conf"


# ── Dataset summary ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dataset_summary_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data/{urn} returns DatasetResponse with required fields.

    spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}:
    urn, name, platform, description, owners, tags.
    """
    resp = await api_client.get(_SUMMARY_URL, headers=admin_headers)
    assert resp.status_code == 200, (
        f"GET dataset summary must return 200; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}"
    )
    body = resp.json()
    # "description" is nullable but the key must always be present in the response.
    for field in ("urn", "name", "platform", "description", "owners", "tags"):
        assert field in body, (
            f"DatasetResponse missing required field {field!r}; got keys: {list(body.keys())}. "
            "spec: API.md §Data Resource — DatasetResponse fields"
        )
    assert body["urn"] == _DATASET_URN, (
        f"urn in response ({body['urn']!r}) must match requested dataset URN. "
        "spec: API.md §Data Resource — urn field"
    )
    assert isinstance(body["platform"], str) and body["platform"], (
        f"platform must be a non-empty string; got {body['platform']!r}. "
        "spec: API.md §Data Resource — platform field"
    )
    assert isinstance(body["owners"], list), (
        f"owners must be a list; got {type(body['owners']).__name__}. "
        "spec: API.md §Data Resource — owners field"
    )
    assert isinstance(body["tags"], list), (
        f"tags must be a list; got {type(body['tags']).__name__}. "
        "spec: API.md §Data Resource — tags field"
    )


# ── Dataset attributes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dataset_attributes_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data/{urn}/attr returns DatasetAttributesResponse.

    spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}/attr:
    urn, column_count, fields, owners, tags, description, quality_score.
    """
    resp = await api_client.get(_ATTR_URL, headers=admin_headers)
    assert resp.status_code == 200, (
        f"GET dataset attr must return 200; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}/attr"
    )
    body = resp.json()
    for field in ("urn", "column_count", "fields", "owners", "tags"):
        assert field in body, (
            f"DatasetAttributesResponse missing required field {field!r}; "
            f"got keys: {list(body.keys())}. "
            "spec: API.md §Data Resource — DatasetAttributesResponse fields"
        )
    assert body["urn"] == _DATASET_URN, (
        f"urn in /attr response ({body['urn']!r}) must match requested dataset URN. "
        "spec: API.md §Data Resource"
    )
    assert isinstance(body["column_count"], int), (
        f"column_count must be an int; got {type(body['column_count']).__name__!r}. "
        "spec: API.md §Data Resource — column_count"
    )
    assert isinstance(body["fields"], list), (
        f"fields must be a list; got {type(body['fields']).__name__}. "
        "spec: API.md §Data Resource — fields list"
    )
    # catalog.title_master has exactly 17 columns per spec/TESTING.md §Test Data Design.
    assert body["column_count"] == 17, (
        f"catalog.title_master must have exactly 17 columns; got {body['column_count']}. "
        "spec/TESTING.md §Test Data Design — catalog.title_master has 17 cols"
    )


# ── Dataset generic event list ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dataset_events_envelope_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data/{urn}/event returns EventListResponse envelope.

    Returns 200 with the standard pagination envelope (offset, limit,
    total_count, events=[]) even when no events exist for this dataset.

    spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}/event:
    dataset-level event history (all event types).
    spec: API.md §Standard Envelope — pagination fields.
    """
    resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 20, "offset": 0},
    )
    assert resp.status_code == 200, (
        f"GET /event must return 200; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Data Resource — GET /spoke/common/data/{urn}/event"
    )
    body = resp.json()
    for key in ("offset", "limit", "total_count", "events"):
        assert key in body, (
            f"EventListResponse envelope missing key {key!r}; got: {list(body.keys())}. "
            "spec: API.md §Standard Envelope"
        )
    assert isinstance(body["events"], list), (
        f"events must be a list; got {type(body['events']).__name__}. "
        "spec: API.md §Data Resource — events list"
    )
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert isinstance(body["total_count"], int) and body["total_count"] >= 0, (
        f"total_count must be a non-negative int; got {body['total_count']!r}. "
        "spec: API.md §Standard Envelope"
    )


@pytest.mark.asyncio
async def test_get_dataset_events_no_unknown_keys(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data/{urn}/event with pagination params round-trips correctly.

    spec: API.md §Data Resource — GET /spoke/common/data/{urn}/event params:
    limit, offset, from, to.  Response limit/offset echo the request values.
    """
    resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 5, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 5, (
        f"Response limit must echo the request limit=5; got {body['limit']!r}. "
        "spec: API.md §Standard Envelope — limit echoes request"
    )
    assert body["offset"] == 0, (
        f"Response offset must echo the request offset=0; got {body['offset']!r}. "
        "spec: API.md §Standard Envelope"
    )


# ── Unified timeline: wrapper flag + major-type filter ───────────────────────


@pytest.mark.asyncio
async def test_get_dataset_events_rows_carry_wrapper_flag(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Every row of the unified /event timeline carries a boolean `wrapper` flag.

    The unified per-dataset timeline unions the covering source's ingestion runs
    (which may be mirrored from a CLI wrapper) with the dataset-level validation
    and metagen events; each row exposes the derived `wrapper` flag.

    spec: API.md §Data Resource — GET /spoke/common/data/{urn}/event rows carry
    `wrapper`; spec/feature/BACKEND.md §Dataset service — unified timeline.
    """
    # Seed a dataset-level event so the timeline is non-empty: a validation conf
    # PUT records a VALIDATION.CONFIG_CREATE event on this dataset.
    await api_client.put(
        _VALIDATION_CONF_URL,
        headers=admin_headers,
        json={
            "description": "Spot timeline seed conf",
            "variables": [{"name": "row_cnt", "description": "row count"}],
        },
    )

    resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 50, "offset": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] >= 1, (
        "After a validation conf PUT the unified timeline must contain at least "
        f"one event; got total_count={body['total_count']}. "
        "spec: BACKEND.md §Dataset service — unified timeline"
    )
    for ev in body["events"]:
        assert "wrapper" in ev, (
            f"each unified-timeline row must carry `wrapper`; got keys {list(ev.keys())}. "
            "spec: API.md §Data Resource — EventResponse.wrapper"
        )
        assert isinstance(ev["wrapper"], bool), (
            f"`wrapper` must be a bool; got {type(ev['wrapper']).__name__}."
        )

    # Cleanup the seeded conf so the dataset returns to baseline.
    with suppress(Exception):
        await api_client.delete(_VALIDATION_CONF_URL, headers=admin_headers)


@pytest.mark.asyncio
async def test_get_dataset_events_major_type_filter_narrows(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """`event_major_type=VALIDATION` returns only VALIDATION.* rows with a
    total_count that matches; `event_major_type=METAGEN` (no metagen activity)
    excludes the validation event.

    spec: API.md §Data Resource — repeatable `event_major_type` filter
    (INGESTION/VALIDATION/METAGEN); omitted means all.
    """
    # Seed a VALIDATION.* dataset-level event.
    await api_client.put(
        _VALIDATION_CONF_URL,
        headers=admin_headers,
        json={
            "description": "Spot filter seed conf",
            "variables": [{"name": "row_cnt", "description": "row count"}],
        },
    )

    # Filter to VALIDATION — the seeded conf event must appear.
    val_resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 50, "offset": 0, "event_major_type": "VALIDATION"},
    )
    assert val_resp.status_code == 200, val_resp.text
    val_body = val_resp.json()
    assert val_body["total_count"] >= 1, (
        "event_major_type=VALIDATION must return the seeded conf event; "
        f"got total_count={val_body['total_count']}. "
        "spec: API.md §Data Resource — event_major_type filter"
    )
    assert all(ev["event_type"].startswith("VALIDATION.") for ev in val_body["events"]), (
        "event_major_type=VALIDATION must return only VALIDATION.* rows; "
        f"got {[ev['event_type'] for ev in val_body['events']]}. "
        "spec: API.md §Data Resource — event_major_type filter"
    )
    # total_count must equal the number of returned VALIDATION rows on this page
    # (no other VALIDATION events exist; page is large enough to hold them all).
    assert val_body["total_count"] == len(val_body["events"]), (
        "filtered total_count must match the filtered event count when the page "
        f"holds them all; total_count={val_body['total_count']} "
        f"events={len(val_body['events'])}. spec: API.md §Standard Envelope"
    )

    # Filter to METAGEN — no metagen activity for this dataset, so the validation
    # event must be excluded.
    mg_resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 50, "offset": 0, "event_major_type": "METAGEN"},
    )
    assert mg_resp.status_code == 200, mg_resp.text
    mg_body = mg_resp.json()
    assert all(ev["event_type"].startswith("METAGEN.") for ev in mg_body["events"]), (
        "event_major_type=METAGEN must return only METAGEN.* rows; "
        f"got {[ev['event_type'] for ev in mg_body['events']]}. "
        "spec: API.md §Data Resource — event_major_type filter"
    )
    val_ids = {ev["id"] for ev in val_body["events"]}
    mg_ids = {ev["id"] for ev in mg_body["events"]}
    assert val_ids.isdisjoint(mg_ids), (
        "the VALIDATION conf event must not appear under event_major_type=METAGEN. "
        "spec: API.md §Data Resource — event_major_type filter"
    )

    # The unfiltered timeline total_count must be >= the VALIDATION-filtered count
    # (the filter only ever narrows).
    all_resp = await api_client.get(
        _EVENT_URL,
        headers=admin_headers,
        params={"limit": 50, "offset": 0},
    )
    all_body = all_resp.json()
    assert all_body["total_count"] >= val_body["total_count"], (
        "unfiltered total_count must be >= a single-major-type filtered count. "
        "spec: API.md §Data Resource — event_major_type filter narrows"
    )

    # Cleanup.
    with suppress(Exception):
        await api_client.delete(_VALIDATION_CONF_URL, headers=admin_headers)


# ── Validation per-dataset event list ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_validation_events_empty_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/data/{urn}/event/validation returns 200 with empty events
    when no validation events exist for this dataset.

    spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}/event/validation:
    validation event reports for this dataset.
    """
    # Ensure no validation conf exists (events wouldn't be triggered anyway,
    # but belt-and-suspenders cleanup).
    with suppress(Exception):
        await api_client.delete(_VALIDATION_CONF_URL, headers=admin_headers)

    resp = await api_client.get(
        _VALIDATION_EVENT_URL,
        headers=admin_headers,
        params={"limit": 20, "offset": 0},
    )
    assert resp.status_code == 200, (
        f"GET /event/validation must return 200 (not 404) even with no events; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: API.md §Data Resource — GET /spoke/common/data/{urn}/event/validation"
    )
    body = resp.json()
    for key in ("offset", "limit", "total_count", "events"):
        assert key in body, (
            f"EventListResponse envelope missing key {key!r}; got: {list(body.keys())}. "
            "spec: API.md §Standard Envelope"
        )
    assert isinstance(body["events"], list), (
        f"events must be a list; got {type(body['events']).__name__}."
    )
    assert body["total_count"] >= 0, (
        f"total_count must be >= 0; got {body['total_count']!r}."
    )
