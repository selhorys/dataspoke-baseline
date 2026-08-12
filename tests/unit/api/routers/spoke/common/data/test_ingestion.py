"""Unit tests for per-dataset ingestion sub-resource routes.

Routes under test (per-source model — read-only per-dataset surface):
  GET /data/{urn}/attr/ingestion         — reverse-lookup (source that covers this dataset)
  GET /data/{urn}/event/ingestion        — ingestion event history for this dataset

The old per-dataset CRUD/run routes are gone in the per-source model.
Source CRUD lives under /spoke/ingestion/sources/{id}.

Spec: API.md §Ingestion — 'GET /data/{urn}/attr/ingestion … Reverse-lookup (read-only)'
Spec: API.md §Data Resource (ingestion rows) — auth gate + HTTP status codes.
Spec: feature/BACKEND.md §Ingestion Service.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_ingestion_service
from src.api.main import app
from src.backend.ingestion.service import IngestionService
from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)
_REVERSE_LOOKUP_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/ingestion"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/ingestion"


@pytest.fixture
def mock_svc() -> AsyncMock:
    """The service double is ``spec``-bound so a renamed or removed service method fails here.

    Without a spec the double answers to any attribute, so a router calling a method the
    service no longer has (``get_latest_run_event``, ``get_events_for_source``) would still
    pass. ``spec_set`` rather than ``spec`` because every test here *assigns* its stub
    (``mock_svc.get_latest_run_event = AsyncMock(...)``): plain ``spec`` restricts reads
    only, so an assignment would re-create the attribute a rename had removed and the guard
    would never fire — verified by renaming the service method and watching a ``spec=``
    fixture stay green.

    spec: TESTING.md §Unit Testing — "Give every shared mock fixture a ``spec=`` … so
    attribute typos and renamed methods fail loud instead of silently returning a new
    auto-mock."
    """
    return AsyncMock(spec_set=IngestionService)


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ingestion_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ingestion_service, None)


# ── Auth gate: 401 without token ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [_REVERSE_LOOKUP_URL, _EVENTS_URL])
async def test_get_route_without_token_returns_401(client, url) -> None:
    """Every ingestion data route rejects an unauthenticated GET.

    Spec: API.md §Authentication — spoke/common routes require valid JWT.
    """
    resp = await client.get(url)
    assert resp.status_code == 401, (
        f"GET {url} without a token must return 401, got {resp.status_code}"
    )


# ── Reverse-lookup: unmapped dataset returns nulls ────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_unmapped_returns_null_source(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/ingestion for a dataset with no owning source returns null source fields.

    Spec: API.md §Ingestion — 'Returns the owning source for a dataset, or null if unmapped'.
    """
    mock_svc.reverse_lookup = AsyncMock(return_value=None)
    mock_svc.get_latest_run_event = AsyncMock(return_value=None)

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["source_id"] is None
    assert body["mode"] is None
    assert body["name"] is None
    assert body["latest_run"] is None


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_mapped_returns_source_info(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/ingestion for a mapped dataset returns the owning source's id, mode, name.

    Spec: API.md §Ingestion — reverse-lookup returns source_id, mode, name.
    """
    source_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    source_record = MagicMock()
    source_record.id = source_id
    source_record.mode = "ACTIVE_CUSTOM_MANAGED"
    source_record.name = "imazon catalog pg"
    source_record.platform = "postgres"
    source_record.schedule = "0 0 * * *"
    source_record.schedule_tier = "daily"
    source_record.recipe = {"source": {"type": "postgres", "config": {}}}
    source_record.datahub_source_urn = None
    source_record.status = "OK"
    source_record.created_at = now
    source_record.updated_at = now

    mock_svc.reverse_lookup = AsyncMock(return_value=source_record)
    mock_svc.get_latest_run_event = AsyncMock(return_value=None)

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["source_id"] == source_id
    assert body["mode"] == "ACTIVE_CUSTOM_MANAGED"
    assert body["name"] == "imazon catalog pg"


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_has_no_schedule_tier(
    client, mock_svc: AsyncMock
) -> None:
    """Reverse-lookup response does NOT expose schedule_tier.

    Spec: BACKEND_SCHEMA.md — schedule_tier is internal, never in the API.
    """
    source_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    source_record = MagicMock()
    source_record.id = source_id
    source_record.mode = "ACTIVE_CUSTOM_MANAGED"
    source_record.name = "test source"
    source_record.platform = "postgres"
    source_record.schedule = "0 0 * * *"
    source_record.schedule_tier = "daily"  # internal — must not appear in response
    source_record.recipe = {"source": {"type": "postgres", "config": {}}}
    source_record.datahub_source_urn = None
    source_record.status = "OK"
    source_record.created_at = now
    source_record.updated_at = now

    mock_svc.reverse_lookup = AsyncMock(return_value=source_record)
    mock_svc.get_latest_run_event = AsyncMock(return_value=None)

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert "schedule_tier" not in body, (
        f"schedule_tier must not appear in the reverse-lookup response. "
        f"Spec: BACKEND_SCHEMA.md — schedule_tier is internal. "
        f"Body keys: {list(body.keys())}"
    )


# ── Event history ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_events_returns_paginated_envelope(
    client, mock_svc: AsyncMock
) -> None:
    """GET /event/ingestion returns a paginated event envelope.

    Spec: API.md §Standard Envelope — events[], total_count, offset, limit.
    """
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))
    mock_svc.reverse_lookup = AsyncMock(return_value=None)

    headers = auth_headers()
    resp = await client.get(_EVENTS_URL, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "total_count" in body


# ── latest_run reads the run-outcome resolver, not the head of the feed ───────


def _mapped_source(source_id: str) -> MagicMock:
    """A covering source record, as ``reverse_lookup`` returns it."""
    now = datetime.now(tz=UTC)
    record = MagicMock()
    record.id = source_id
    record.mode = "ACTIVE_CUSTOM_MANAGED"
    record.name = "imazon catalog pg"
    record.platform = "postgres"
    record.schedule = "0 0 * * *"
    record.schedule_tier = "daily"
    record.recipe = {"source": {"type": "postgres", "config": {}}}
    record.datahub_source_urn = None
    record.status = "OK"
    record.created_at = now
    record.updated_at = now
    return record


@pytest.mark.asyncio
async def test_latest_run_comes_from_the_run_outcome_resolver_not_the_raw_feed(
    client, mock_svc: AsyncMock
) -> None:
    """``latest_run`` reports the resolved run outcome, not the newest event in the feed.

    The feed is deliberately stubbed with a *newer* per-dataset observation than the run
    outcome, so a route that took the head of the feed would report ``success`` where the
    source's last run failed. The unfiltered feed is also asserted untouched: it is the
    timeline's read, and reading it here is exactly the defect.

    Spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "**Source ``latest_run`` =
      latest terminal *run* outcome**, over run-level producers only … a **``detail.source``
      blacklist** of the observation producers, so a newer per-dataset ``COMPLETE`` cannot
      outrank an older run ``FAIL``."
    Spec: feature/BACKEND.md §Run-event consumption — "'the newest event for this source'
      is not 'the newest run', which is why ``latest_run`` filters on producer rather than
      taking the head of the feed."
    """
    source_id = str(uuid.uuid4())
    failed_at = datetime.now(tz=UTC) - timedelta(hours=2)
    observed_at = datetime.now(tz=UTC) - timedelta(minutes=5)

    mock_svc.reverse_lookup = AsyncMock(return_value=_mapped_source(source_id))
    # The resolver's answer: the source's last RUN failed two hours ago.
    mock_svc.get_latest_run_event = AsyncMock(
        return_value={
            "id": str(uuid.uuid4()),
            "entity_type": "ingestion_source",
            "entity_id": source_id,
            "event_type": "INGESTION.FAIL",
            "status": "error",
            "detail": {"run_id": "run-42", "platform": "postgres"},
            "occurred_at": failed_at,
            "wrapper": False,
        }
    )
    # The raw feed's head is a NEWER per-dataset observation carrying status="success".
    mock_svc.get_events_for_source = AsyncMock(
        return_value=(
            [
                {
                    "id": str(uuid.uuid4()),
                    "entity_type": "ingestion_source",
                    "entity_id": source_id,
                    "event_type": "INGESTION.COMPLETE",
                    "status": "success",
                    "detail": {
                        "source": "last_ingested_observation",
                        "dataset_urn": _VALID_URN,
                    },
                    "occurred_at": observed_at,
                    "wrapper": False,
                }
            ],
            1,
        )
    )

    resp = await client.get(_REVERSE_LOOKUP_URL, headers=auth_headers())
    assert resp.status_code == 200

    latest_run = resp.json()["latest_run"]
    assert latest_run is not None, (
        "the resolver returned a run outcome, so latest_run must not be null."
    )
    assert latest_run["status"] == "error", (
        f"latest_run must report the resolved run outcome (the FAIL), not the newer "
        f"observation in the feed; got {latest_run!r}. "
        "Spec: feature/BACKEND.md §Sync + mapping sweep step 4 — Source latest_run."
    )
    assert latest_run["run_id"] == "run-42"
    mock_svc.get_events_for_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_source_with_no_run_outcome_reports_a_null_latest_run(
    client, mock_svc: AsyncMock
) -> None:
    """A source whose only events are observations reports ``latest_run: null``.

    This is the `PASSIVE` reading: neither run-level producer covers that mode, so its
    feed holds only per-dataset observations and there is no run outcome to report. The
    feed is seeded non-empty so the null is the resolver's verdict rather than an absence
    of data.

    Spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "**A ``PASSIVE`` source
      reports no ``latest_run``, by construction.** … ``attr/ingestion.latest_run`` is
      ``null`` for it. That is the intended reading and not a missing signal".
    """
    source_id = str(uuid.uuid4())
    mock_svc.reverse_lookup = AsyncMock(return_value=_mapped_source(source_id))
    mock_svc.get_latest_run_event = AsyncMock(return_value=None)
    mock_svc.get_events_for_source = AsyncMock(
        return_value=(
            [
                {
                    "id": str(uuid.uuid4()),
                    "entity_type": "ingestion_source",
                    "entity_id": source_id,
                    "event_type": "INGESTION.COMPLETE",
                    "status": "success",
                    "detail": {
                        "source": "passive_observation",
                        "dataset_urn": _VALID_URN,
                        "operation_type": "INSERT",
                    },
                    "occurred_at": datetime.now(tz=UTC),
                    "wrapper": False,
                }
            ],
            1,
        )
    )

    resp = await client.get(_REVERSE_LOOKUP_URL, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == source_id, (
        "backstop: the source must still be reported as the owner, so the null below is "
        "about the run outcome and not about an unmapped dataset."
    )
    assert body["latest_run"] is None, (
        f"a feed carrying only observations must produce latest_run=null; got "
        f"{body['latest_run']!r}. Spec: feature/BACKEND.md §Sync + mapping sweep step 4."
    )


@pytest.mark.asyncio
async def test_event_ingestion_narrows_the_source_feed_to_this_dataset(
    client, mock_svc: AsyncMock
) -> None:
    """``GET /event/ingestion`` requests the source feed narrowed to this dataset URN.

    Without the narrowing, a source covering many datasets projects every sibling's
    per-dataset observations onto this dataset's timeline.

    Spec: feature/BACKEND.md §Querying Events — the per-dataset timeline resolves the
      source's rows "by reverse-lookup plus the ``detail.dataset_urn`` predicate".
    """
    source_id = str(uuid.uuid4())
    mock_svc.reverse_lookup = AsyncMock(return_value=_mapped_source(source_id))
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))

    resp = await client.get(_EVENTS_URL, headers=auth_headers())
    assert resp.status_code == 200

    mock_svc.get_events_for_source.assert_awaited_once()
    kwargs = mock_svc.get_events_for_source.await_args.kwargs
    assert kwargs.get("dataset_urn") == _VALID_URN, (
        f"the source feed must be requested narrowed to this dataset URN, by keyword; got "
        f"kwargs={kwargs!r}. Spec: feature/BACKEND.md §Querying Events."
    )
