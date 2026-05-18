"""Unit tests for internal activity endpoints (Airflow callback receiver).

Routes under test:
  POST /internal/activities/ingestion/list-active
  POST /internal/activities/ingestion/run
  POST /internal/activities/ingestion/passive-sync
  POST /internal/activities/metagen/run
  POST /internal/activities/metrics/list-active
  POST /internal/activities/metrics/run
  POST /internal/activities/ontogen/run
  POST /internal/activities/datahub/sync

spec: API.md §Internal routes — X-Internal-Token header required.
spec: feature/BACKEND.md §DAG Catalogue + §Dependency Injection — activity endpoints
      accept documented payload shapes and return 400 (non-retryable) / 500 (retryable).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import DataSpokeError
from tests.unit.api.conftest import auth_headers

_INTERNAL_TOKEN = "test-internal-secret-act"

_INGESTION_LIST = "/internal/activities/ingestion/list-active"
_INGESTION_RUN = "/internal/activities/ingestion/run"
_INGESTION_PASSIVE = "/internal/activities/ingestion/passive-sync"
_METAGEN_RUN = "/internal/activities/metagen/run"
_METRICS_LIST = "/internal/activities/metrics/list-active"
_METRICS_RUN = "/internal/activities/metrics/run"
_ONTOGEN_RUN = "/internal/activities/ontogen/run"
_DATAHUB_SYNC = "/internal/activities/datahub/sync"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── Shared helper ─────────────────────────────────────────────────────────────


def _internal_headers() -> dict:
    return {"X-Internal-Token": _INTERNAL_TOKEN}


# ── Auth gate: 401 without X-Internal-Token ───────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_list_active_without_token_returns_401(client) -> None:
    """POST /internal/activities/ingestion/list-active without token returns 401.

    spec: API.md §Internal routes — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_INGESTION_LIST, json={"tier": "daily"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_metrics_list_active_without_token_returns_401(client) -> None:
    """POST /internal/activities/metrics/list-active without token returns 401.

    spec: API.md §Internal routes — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_METRICS_LIST, json={"tier": "daily"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ontogen_run_without_token_returns_401(client) -> None:
    """POST /internal/activities/ontogen/run without token returns 401.

    spec: API.md §Internal routes — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_ONTOGEN_RUN, json={"dry_run": False})
    assert resp.status_code == 401


# ── Happy path: ingestion/list-active ────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_list_active_returns_list_of_urns(client) -> None:
    """POST /internal/activities/ingestion/list-active returns a list of URN strings.

    spec: feature/BACKEND.md §Ingestion Workflow — list-active returns URNs for tier.
    """
    mock_svc = AsyncMock()
    mock_svc.list_active_for_tier = AsyncMock(return_value=[_VALID_URN])

    from unittest.mock import AsyncMock as AM, MagicMock as MM

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch(
            "src.backend.ingestion.service.IngestionService.list_active_for_tier",
            new=AsyncMock(return_value=[_VALID_URN]),
        ),
    ):
        resp = await client.post(
            _INGESTION_LIST,
            json={"tier": "daily"},
            headers=_internal_headers(),
        )

    # The endpoint may succeed or DataSpokeError → 200 with list either way
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Happy path: ingestion/run ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_run_accepts_documented_payload(client) -> None:
    """POST /internal/activities/ingestion/run accepts {dataset_urn, dry_run} payload.

    spec: feature/BACKEND.md §Ingestion Workflow — /ingestion/run accepts the documented shape.
    The endpoint catches DataSpokeError and returns a structured error dict (not 422).
    We stub the service.run call to raise DataSpokeError so the endpoint handles it gracefully.
    """
    import src.backend.ingestion.service as _ing_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    _ds_error = DataSpokeError("stubbed ingestion failure")

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_ing_svc.IngestionService, "run", new=AsyncMock(side_effect=_ds_error)),
    ):
        resp = await client.post(
            _INGESTION_RUN,
            json={"dataset_urn": _VALID_URN, "dry_run": True},
            headers=_internal_headers(),
        )

    # Valid payload → no 422 (schema validation must pass before service is called)
    assert resp.status_code != 422, (
        f"Valid payload triggered 422; got: {resp.text}"
    )


# ── Payload shape: reject malformed input ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_list_active_missing_tier_returns_422(client) -> None:
    """POST /internal/activities/ingestion/list-active without 'tier' returns 422.

    spec: feature/BACKEND.md §Ingestion Workflow — tier is a required field.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(
            _INGESTION_LIST,
            json={},  # missing required 'tier'
            headers=_internal_headers(),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_metagen_run_tier_mismatch_short_circuits_without_invoking_run(client) -> None:
    """POST /internal/activities/metagen/run with tier!=conf.schedule_tier short-circuits.

    spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection — "For singleton-conf
    features (ontogen, metagen), only the tier listed on the singleton conf runs at
    that tier (the other two tier DAGs short-circuit when triggered)."

    The activity must NOT invoke service.run when the requested tier does not match
    the singleton conf's schedule_tier — otherwise periodic DAGs over-run.
    """
    import src.backend.metagen.service as _mg_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"

    run_mock = AsyncMock()

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_vector", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_mg_svc.MetagenService, "get_global_conf", new=AsyncMock(return_value=fake_conf)),
        patch.object(_mg_svc.MetagenService, "run", new=run_mock),
    ):
        resp = await client.post(
            _METAGEN_RUN,
            json={"tier": "hourly", "dry_run": False},
            headers=_internal_headers(),
        )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "tier_mismatch"
    assert body["dag_tier"] == "hourly"
    assert body["conf_tier"] == "daily"
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_metagen_run_tier_match_invokes_run(client) -> None:
    """POST /internal/activities/metagen/run with tier==conf.schedule_tier invokes service.run.

    spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection — the DAG whose tier
    matches the singleton conf is the one that actually performs inference.
    """
    import src.backend.metagen.service as _mg_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"

    _ds_error = DataSpokeError("stubbed metagen failure")
    run_mock = AsyncMock(side_effect=_ds_error)

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_vector", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_mg_svc.MetagenService, "get_global_conf", new=AsyncMock(return_value=fake_conf)),
        patch.object(_mg_svc.MetagenService, "run", new=run_mock),
    ):
        resp = await client.post(
            _METAGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    run_mock.assert_called_once()
    assert resp.status_code != 422, f"unexpected 422: {resp.text}"


@pytest.mark.asyncio
async def test_ontogen_run_tier_mismatch_short_circuits_without_invoking_run(client) -> None:
    """POST /internal/activities/ontogen/run with tier!=conf.schedule_tier short-circuits.

    spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection — "For singleton-conf
    features (ontogen, metagen), only the tier listed on the singleton conf runs at
    that tier (the other two tier DAGs short-circuit when triggered)."

    The activity must NOT invoke service.run when the requested tier does not match
    the singleton conf's schedule_tier — otherwise periodic DAGs over-run.
    """
    import src.backend.ontogen.service as _onto_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"

    run_mock = AsyncMock()

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_vector", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_onto_svc.OntogenService, "get_conf", new=AsyncMock(return_value=fake_conf)),
        patch.object(_onto_svc.OntogenService, "run", new=run_mock),
    ):
        resp = await client.post(
            _ONTOGEN_RUN,
            json={"tier": "hourly", "dry_run": False},
            headers=_internal_headers(),
        )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "tier_mismatch"
    assert body["dag_tier"] == "hourly"
    assert body["conf_tier"] == "daily"
    # The whole point: service.run was NOT called — no Redis lock, no inference.
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ontogen_run_tier_match_invokes_run(client) -> None:
    """POST /internal/activities/ontogen/run with tier==conf.schedule_tier invokes service.run.

    spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection — the DAG whose tier
    matches the singleton conf is the one that actually performs inference.
    """
    import src.backend.ontogen.service as _onto_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"

    _ds_error = DataSpokeError("stubbed ontogen failure")
    run_mock = AsyncMock(side_effect=_ds_error)

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_vector", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_onto_svc.OntogenService, "get_conf", new=AsyncMock(return_value=fake_conf)),
        patch.object(_onto_svc.OntogenService, "run", new=run_mock),
    ):
        resp = await client.post(
            _ONTOGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    # Service was invoked (even though it errored out via the stub) — proves the
    # tier check did NOT short-circuit when the tiers matched.
    run_mock.assert_called_once()
    # The DataSpokeError is mapped to a structured error response (200/400/500 envelope),
    # not 422 — schema is valid.
    assert resp.status_code != 422, f"unexpected 422: {resp.text}"


@pytest.mark.asyncio
async def test_ontogen_run_accepts_optional_prompt_md(client) -> None:
    """POST /internal/activities/ontogen/run accepts optional prompt_md field.

    spec: feature/BACKEND.md §Ontogen Workflow — ontogen/run accepts {dry_run, prompt_md}.
    The endpoint catches DataSpokeError and returns a structured dict (not 422).
    We stub the service to raise DataSpokeError so schema acceptance is verified cleanly.
    """
    import src.backend.ontogen.service as _onto_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    _ds_error = DataSpokeError("stubbed ontogen failure")

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_cache", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_vector", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch.object(_onto_svc.OntogenService, "run", new=AsyncMock(side_effect=_ds_error)),
    ):
        resp = await client.post(
            _ONTOGEN_RUN,
            json={"dry_run": True, "prompt_md": None},
            headers=_internal_headers(),
        )

    # Not a 422 — payload shape is valid (schema validation runs before service call)
    assert resp.status_code != 422, (
        f"Valid payload with optional prompt_md triggered 422; got: {resp.text}"
    )
