"""Unit tests for internal activity endpoints (Airflow callback receiver).

Routes under test:
  POST /internal/activities/ingestion/list-active
  POST /internal/activities/ingestion/run
  POST /internal/activities/ingestion/sync
  POST /internal/activities/metagen/run
  POST /internal/activities/metrics/list-active
  POST /internal/activities/metrics/run
  POST /internal/activities/ontogen/run

spec: API.md §Internal Activities — X-Internal-Token header required.
spec: feature/BACKEND.md §DAG Catalogue + §Dependency Injection — activity endpoints
      accept documented payload shapes and return 400 (non-retryable) / 500 (retryable).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import DataSpokeError

_INTERNAL_TOKEN = "test-internal-secret-act"

_INGESTION_LIST = "/internal/activities/ingestion/list-active"
_INGESTION_RUN = "/internal/activities/ingestion/run"
_INGESTION_SYNC = "/internal/activities/ingestion/sync"
_METAGEN_RUN = "/internal/activities/metagen/run"
_METRICS_LIST = "/internal/activities/metrics/list-active"
_METRICS_RUN = "/internal/activities/metrics/run"
_ONTOGEN_RUN = "/internal/activities/ontogen/run"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── Shared helper ─────────────────────────────────────────────────────────────


def _internal_headers() -> dict:
    return {"X-Internal-Token": _INTERNAL_TOKEN}


# ── Auth gate: 401 without X-Internal-Token ───────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_list_active_without_token_returns_401(client) -> None:
    """POST /internal/activities/ingestion/list-active without token returns 401.

    spec: API.md §Internal Activities — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_INGESTION_LIST, json={"tier": "daily"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_metrics_list_active_without_token_returns_401(client) -> None:
    """POST /internal/activities/metrics/list-active without token returns 401.

    spec: API.md §Internal Activities — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_METRICS_LIST, json={"tier": "daily"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ontogen_run_without_token_returns_401(client) -> None:
    """POST /internal/activities/ontogen/run without token returns 401.

    spec: API.md §Internal Activities — X-Internal-Token required.
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
    mock_svc.list_active_sources_for_tier = AsyncMock(return_value=[])


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
            "src.backend.ingestion.service.IngestionService.list_active_sources_for_tier",
            new=AsyncMock(return_value=[]),
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
    """POST /internal/activities/ingestion/run accepts {source_id, dry_run} payload.

    spec: feature/BACKEND.md §Ingestion Workflow — /ingestion/run accepts source_id
    (per-source model — not dataset_urn).
    The endpoint catches DataSpokeError and returns a structured error dict (not 422).
    We stub the service.run call to raise DataSpokeError so the endpoint handles it gracefully.
    """
    import uuid

    import src.backend.ingestion.service as _ing_svc

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    _ds_error = DataSpokeError("stubbed ingestion failure")
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    test_source_id = str(uuid.uuid4())

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(_ing_svc.IngestionService, "run", new=AsyncMock(side_effect=_ds_error)),
    ):
        resp = await client.post(
            _INGESTION_RUN,
            json={"source_id": test_source_id, "dry_run": True},
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


def _make_metagen_conf_dto(*, conf_id: str, schedule_tier: str | None, is_enabled: bool = True):
    """Build a MetagenConfDTO for the fan-out activity tests."""
    from datetime import UTC, datetime

    from src.backend.metagen.service import MetagenConfDTO

    return MetagenConfDTO(
        id=conf_id,
        name=f"conf-{conf_id[:4]}",
        is_enabled=is_enabled,
        schedule_tier=schedule_tier,
        dataset_filter={},
        result_limit=3,
        overwrite_pending=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_metagen_run_fans_out_only_to_confs_matching_tier(client) -> None:
    """POST /internal/activities/metagen/run runs only enabled confs whose schedule_tier
    matches the fired tier; confs at other tiers are not run.

    spec: feature/BACKEND.md §DAG Catalogue (Tier-DAG selection) + §Concurrency Guards —
    the activity enumerates every is_enabled=true conf whose schedule_tier matches the
    fired tier and runs each under its own per-conf metagen:running:{conf_id} lock.
    """
    import uuid as _uuid

    import src.backend.metagen.service as _mg_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO
    from src.backend.metagen.service import RunResultDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    daily_conf_id = str(_uuid.uuid4())
    hourly_conf_id = str(_uuid.uuid4())
    confs = [
        _make_metagen_conf_dto(conf_id=daily_conf_id, schedule_tier="daily"),
        _make_metagen_conf_dto(conf_id=hourly_conf_id, schedule_tier="hourly"),
    ]
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)

    ran_conf_ids: list[str] = []

    async def fake_run(self, conf_id, *, dataset_urns=None, dry_run=False):
        ran_conf_ids.append(conf_id)
        return RunResultDTO(
            run_id=str(_uuid.uuid4()),
            conf_id=conf_id,
            status="success",
            dry_run=dry_run,
            unresolved_urns=[],
            counts={"items_considered": 0},
        )

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(
            _mg_svc.MetagenService, "list_confs", new=AsyncMock(return_value=(confs, len(confs)))
        ),
        patch.object(_mg_svc.MetagenService, "run", new=fake_run),
    ):
        resp = await client.post(
            _METAGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert ran_conf_ids == [daily_conf_id], (
        "Only the daily conf must run when tier=daily. "
        "spec: feature/BACKEND.md §DAG Catalogue (Tier-DAG selection) + §Concurrency Guards"
    )
    body = resp.json()
    assert body["conf_count"] == 1


@pytest.mark.asyncio
async def test_metagen_run_continues_past_a_failing_conf(client) -> None:
    """POST /internal/activities/metagen/run aggregates per-conf results and continues
    past a conf that fails (does not abort the whole tier).

    spec: feature/BACKEND.md §DAG Catalogue (Tier-DAG selection) + §Concurrency Guards —
    per-conf results are aggregated and a failing conf does not abort the rest of the tier.
    """
    import uuid as _uuid

    import src.backend.metagen.service as _mg_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO
    from src.backend.metagen.service import RunResultDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    failing_id = str(_uuid.uuid4())
    ok_id = str(_uuid.uuid4())
    confs = [
        _make_metagen_conf_dto(conf_id=failing_id, schedule_tier="daily"),
        _make_metagen_conf_dto(conf_id=ok_id, schedule_tier="daily"),
    ]
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)

    async def fake_run(self, conf_id, *, dataset_urns=None, dry_run=False):
        if conf_id == failing_id:
            raise DataSpokeError("stubbed metagen failure")
        return RunResultDTO(
            run_id=str(_uuid.uuid4()),
            conf_id=conf_id,
            status="success",
            dry_run=dry_run,
            unresolved_urns=[],
            counts={"items_considered": 0},
        )

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(
            _mg_svc.MetagenService, "list_confs", new=AsyncMock(return_value=(confs, len(confs)))
        ),
        patch.object(_mg_svc.MetagenService, "run", new=fake_run),
    ):
        resp = await client.post(
            _METAGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["conf_count"] == 2
    statuses = {r["conf_id"]: r["status"] for r in body["results"]}
    # INTERNAL-contract check (not a public API.md invariant): the
    # /internal/activities/metagen/run aggregated-results shape is internal to the
    # Airflow tier-DAG fan-out and is not part of API.md's public catalogue. The
    # literal "failed" string is the internal activity's own per-conf marker.
    assert statuses[failing_id] == "failed", (
        "The failing conf must be recorded as failed in the aggregated results "
        "(internal activity contract; not a public API.md route)."
    )
    # The OK conf must have been reached and run despite the earlier failure — its
    # aggregated entry carries the successful run outcome, not 'failed'.
    assert statuses[ok_id] != "failed", (
        "A failing conf must not abort the rest of the tier; the OK conf still runs. "
        "spec: feature/BACKEND.md §DAG Catalogue (Tier-DAG selection) + §Concurrency Guards"
    )


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
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)

    run_mock = AsyncMock()

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
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
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)

    _ds_error = DataSpokeError("stubbed ontogen failure")
    run_mock = AsyncMock(side_effect=_ds_error)

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
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

    spec: feature/BACKEND.md §Ontology Generation Service — ontogen/run accepts
    {dry_run, prompt_md}.
    The endpoint catches DataSpokeError and returns a structured dict (not 422).
    We stub the service to raise DataSpokeError so schema acceptance is verified cleanly.
    """
    import src.backend.ontogen.service as _onto_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    _ds_error = DataSpokeError("stubbed ontogen failure")

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=AsyncMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
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


# ── Stub-flag threading assertions ────────────────────────────────────────────
#
# These tests verify that each activity endpoint passes the RuntimeConfigDTO stub_*
# field through to the matching factory call.  A regression where the activity
# calls make_redis_client() with no kwargs (using default stub=False) instead of
# make_redis_client(stub=rc.stub_redis_client) would pass the existing tests but
# fail these three.
#
# spec: src/workflows/_common.py — factories accept stub= kwarg; callers must
#       thread the RuntimeConfigDTO value through.


@pytest.mark.asyncio
async def test_ingestion_run_threads_stub_redis_flag(client) -> None:
    """ingestion/run calls make_redis_client(stub=rc.stub_redis_client).

    Patches get_runtime_config to return stub_redis_client=True, then asserts
    make_redis_client was called with stub=True — not with no kwargs or stub=False.

    spec: src/workflows/_common.py — make_redis_client(stub=...) sourced from RuntimeConfigDTO.
    spec: feature/BACKEND.md §Dependency Injection — activity endpoints thread stub flags.
    """
    import src.backend.ingestion.service as _ing_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_rc = RuntimeConfigDTO(**{**RUNTIME_CONFIG_DEFAULTS, "stub_redis_client": True})
    make_redis_mock = MagicMock(return_value=AsyncMock())

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", make_redis_mock),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(
            _ing_svc.IngestionService,
            "run",
            new=AsyncMock(side_effect=DataSpokeError("stub")),
        ),
    ):
        import uuid as _uuid
        await client.post(
            _INGESTION_RUN,
            json={"source_id": str(_uuid.uuid4()), "dry_run": True},
            headers=_internal_headers(),
        )

    assert make_redis_mock.call_args is not None, (
        "ingestion/run did not call make_redis_client at all."
    )
    assert make_redis_mock.call_args.kwargs.get("stub") is True, (
        f"ingestion/run must call make_redis_client(stub=True) when rc.stub_redis_client=True; "
        f"actual call: {make_redis_mock.call_args}. "
        "spec: src/workflows/_common.py — stub= kwarg must be threaded from RuntimeConfigDTO."
    )


@pytest.mark.asyncio
async def test_metagen_run_threads_stub_llm_flag(client) -> None:
    """metagen/run calls make_llm_client(stub=rc.stub_llm_client, ...).

    Patches get_runtime_config to return stub_llm_client=True, then asserts
    make_llm_client was called with stub=True.

    spec: src/workflows/_common.py — make_llm_client(stub=...) sourced from RuntimeConfigDTO.
    spec: feature/BACKEND.md §Dependency Injection — activity endpoints thread stub flags.
    """
    import src.backend.metagen.service as _mg_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    import uuid as _uuid

    fake_rc = RuntimeConfigDTO(**{**RUNTIME_CONFIG_DEFAULTS, "stub_llm_client": True})
    make_llm_mock = MagicMock(return_value=MagicMock())

    confs = [_make_metagen_conf_dto(conf_id=str(_uuid.uuid4()), schedule_tier="daily")]

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_llm_client", make_llm_mock),
        patch(
            "src.api.routers.internal.activities.make_pgvector_manager",
            return_value=MagicMock(),
        ),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(
            _mg_svc.MetagenService, "list_confs", new=AsyncMock(return_value=(confs, len(confs)))
        ),
        patch.object(
            _mg_svc.MetagenService,
            "run",
            new=AsyncMock(side_effect=DataSpokeError("stub")),
        ),
    ):
        await client.post(
            _METAGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    assert make_llm_mock.call_args is not None, (
        "metagen/run did not call make_llm_client at all."
    )
    assert make_llm_mock.call_args.kwargs.get("stub") is True, (
        f"metagen/run must call make_llm_client(stub=True) when rc.stub_llm_client=True; "
        f"actual call: {make_llm_mock.call_args}. "
        "spec: src/workflows/_common.py — stub= kwarg must be threaded from RuntimeConfigDTO."
    )


@pytest.mark.asyncio
async def test_ontogen_run_threads_stub_pgvector_flag(client) -> None:
    """ontogen/run calls make_pgvector_manager(stub=rc.stub_pgvector_manager).

    Patches get_runtime_config to return stub_pgvector_manager=True, then asserts
    make_pgvector_manager was called with stub=True.

    spec: src/workflows/_common.py — make_pgvector_manager(stub=...) sourced from RuntimeConfigDTO.
    spec: feature/BACKEND.md §Dependency Injection — activity endpoints thread stub flags.
    """
    import src.backend.ontogen.service as _onto_svc
    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    class _FakeSession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            pass

    fake_rc = RuntimeConfigDTO(**{**RUNTIME_CONFIG_DEFAULTS, "stub_pgvector_manager": True})
    make_pgvector_mock = MagicMock(return_value=MagicMock())

    fake_conf = MagicMock()
    fake_conf.schedule_tier = "daily"

    with (
        patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
        patch("src.api.routers.internal.activities.make_datahub", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_redis_client", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_llm_client", return_value=MagicMock()),
        patch("src.api.routers.internal.activities.make_pgvector_manager", make_pgvector_mock),
        patch("src.api.routers.internal.activities.make_db_session", return_value=_FakeSession()),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ),
        patch.object(_onto_svc.OntogenService, "get_conf", new=AsyncMock(return_value=fake_conf)),
        patch.object(
            _onto_svc.OntogenService,
            "run",
            new=AsyncMock(side_effect=DataSpokeError("stub")),
        ),
    ):
        await client.post(
            _ONTOGEN_RUN,
            json={"tier": "daily", "dry_run": False},
            headers=_internal_headers(),
        )

    assert make_pgvector_mock.call_args is not None, (
        "ontogen/run did not call make_pgvector_manager at all."
    )
    assert make_pgvector_mock.call_args.kwargs.get("stub") is True, (
        f"ontogen/run must call make_pgvector_manager(stub=True) when stub_pgvector_manager=True; "
        f"actual call: {make_pgvector_mock.call_args}. "
        "spec: src/workflows/_common.py — stub= kwarg must be threaded from RuntimeConfigDTO."
    )
