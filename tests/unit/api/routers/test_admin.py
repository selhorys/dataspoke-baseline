"""Unit tests for admin router endpoints.

Routes under test:
  POST /api/v1/admin/dags/verify       — requires Admin role
  POST /internal/admin/dags/verify     — requires X-Internal-Token header
  POST /internal/admin/datahub/sync    — requires X-Internal-Token header

spec: API.md §Access Control — Admin role required for /admin/*
spec: API.md §Internal Admin (/internal/admin) — X-Internal-Token required for /internal/…
spec: feature/BACKEND.md §DAG Catalogue — verify returns found/missing/total_expected.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.dependencies import get_airflow_client, get_datahub, get_db
from src.api.main import app
from tests.unit.api.conftest import auth_headers

_ADMIN_VERIFY = "/api/v1/admin/dags/verify"
_INTERNAL_VERIFY = "/internal/admin/dags/verify"
_INTERNAL_SYNC = "/internal/admin/datahub/sync"

_INTERNAL_TOKEN = "test-internal-secret"


# ── Auth: JWT gate for /admin/dags/verify ────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_dags_without_token_returns_401(client) -> None:
    """POST /admin/dags/verify without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.post(_ADMIN_VERIFY)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_verify_dags_non_admin_role_returns_403(client) -> None:
    """POST /admin/dags/verify with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext
    from tests.unit.api.conftest import _make_mock_user

    reader_user = _make_mock_user(role="Reader")
    reader_ctx = AuthContext(user=reader_user, effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.post(_ADMIN_VERIFY, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


@pytest.mark.asyncio
async def test_verify_dags_admin_role_returns_200(client) -> None:
    """POST /admin/dags/verify with Admin role returns 200 with DAG report.

    spec: feature/BACKEND.md §DAG Catalogue — response contains found, missing, total_expected.
    """
    from src.api.auth.dependencies import require_admin, require_authenticated
    from src.backend.auth.privilege import AuthContext
    from tests.unit.api.conftest import _make_mock_user

    admin_user = _make_mock_user(role="Admin")
    admin_ctx = AuthContext(user=admin_user, effective_role="Admin")
    mock_airflow = AsyncMock()
    mock_airflow.list_dags = AsyncMock(return_value=[])

    app.dependency_overrides[require_authenticated] = lambda: admin_ctx
    app.dependency_overrides[require_admin] = lambda: admin_ctx
    app.dependency_overrides[get_airflow_client] = lambda: mock_airflow
    try:
        resp = await client.post(_ADMIN_VERIFY, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(require_authenticated, None)
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(get_airflow_client, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "found" in body
    assert "missing" in body
    assert "total_expected" in body


# ── Internal token gate for /internal/admin/* ─────────────────────────────────


@pytest.mark.asyncio
async def test_internal_verify_dags_without_token_returns_401(client) -> None:
    """POST /internal/admin/dags/verify without X-Internal-Token returns 401.

    spec: API.md §Internal Admin (/internal/admin) — X-Internal-Token shared-secret required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_INTERNAL_VERIFY)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_verify_dags_wrong_token_returns_401(client) -> None:
    """POST /internal/admin/dags/verify with wrong token returns 401.

    spec: API.md §Internal Admin (/internal/admin) — constant-time compare; mismatch → 401.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(
            _INTERNAL_VERIFY,
            headers={"X-Internal-Token": "wrong-token"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_verify_dags_correct_token_returns_200(client) -> None:
    """POST /internal/admin/dags/verify with correct X-Internal-Token returns 200.

    spec: API.md §Internal Admin (/internal/admin) — valid token grants access.
    """
    mock_airflow = AsyncMock()
    mock_airflow.list_dags = AsyncMock(return_value=[])

    app.dependency_overrides[get_airflow_client] = lambda: mock_airflow
    try:
        with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
            resp = await client.post(
                _INTERNAL_VERIFY,
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_airflow_client, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "found" in body
    assert "missing" in body


@pytest.mark.asyncio
async def test_internal_datahub_sync_without_token_returns_401(client) -> None:
    """POST /internal/admin/datahub/sync without X-Internal-Token returns 401.

    spec: API.md §Internal Admin (/internal/admin) — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.post(_INTERNAL_SYNC)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_datahub_sync_correct_token_returns_200(client) -> None:
    """POST /internal/admin/datahub/sync with correct token returns 200.

    spec: API.md §Internal Admin (/internal/admin) — valid token grants access to datahub sync.
    """
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    sync_result = {
        "checked": 5,
        "flipped_true": 2,
        "flipped_false": 0,
        "unchanged": 3,
        "not_found": 0,
    }

    async def _fake_db():
        yield mock_db

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_datahub] = lambda: MagicMock()

    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            # The admin router imports sync_with_datahub at module level, so patch at that path.
            patch("src.api.routers.admin.sync_with_datahub", AsyncMock(return_value=sync_result)),
        ):
            resp = await client.post(
                _INTERNAL_SYNC,
                json={"dataset_urns": None},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_datahub, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "checked" in body
