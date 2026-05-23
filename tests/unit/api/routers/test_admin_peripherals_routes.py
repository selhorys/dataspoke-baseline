"""Unit tests for admin peripheral routes.

Routes under test:
  GET  /api/v1/admin/peripherals                — requires 'admin' group JWT
  GET  /api/v1/admin/peripherals/datahub        — requires 'admin' group JWT
  PATCH /api/v1/admin/peripherals/datahub       — requires 'admin' group JWT
  GET  /api/v1/admin/peripherals/langfuse       — requires 'admin' group JWT
  PATCH /api/v1/admin/peripherals/langfuse      — requires 'admin' group JWT
  PATCH /internal/admin/peripherals/datahub     — requires X-Internal-Token
  PATCH /internal/admin/peripherals/langfuse    — requires X-Internal-Token

Concerns covered:

1. Auth gates:
   - GET  /admin/peripherals without JWT → 401
   - GET  /admin/peripherals with non-admin group → 403
   - PATCH /admin/peripherals/datahub without JWT → 401
   - PATCH /admin/peripherals/datahub with non-admin group → 403
   - PATCH /internal/admin/peripherals/datahub without X-Internal-Token → 401
   - PATCH /internal/admin/peripherals/datahub with wrong token → 401

2. GET /admin/peripherals:
   - Returns 200 with is_configured per peripheral.
   - is_configured=True iff dto is not None AND secret is set.
   - is_configured=False when dto is None.
   - is_configured=False when dto present but secret unset.

3. GET /admin/peripherals/datahub:
   - Returns 200 with masked token ("********") when set.
   - Returns 200 with token="" when unset.
   - Returns 200 with empty fields when unconfigured (dto is None).

4. PATCH /admin/peripherals/datahub:
   - Token is routed to Secret FIRST; DB patch is skipped if Secret write fails → 503.
   - Token="" clears the secret.
   - Omitting token field does not alter the secret.
   - gms_url + kafka_brokers are written to DB.

5. GET /admin/peripherals/langfuse / PATCH /admin/peripherals/langfuse:
   - Mirrors datahub coverage for secret_key masking, 503 on write failure, clear.

6. PATCH /internal/admin/peripherals/datahub with correct token → 200.
7. PATCH /internal/admin/peripherals/langfuse with correct token → 200.

Spec traceability:
- plan/scalable-beaming-hamster.md §Peripheral configuration — is_configured predicate.
- spec/API.md §Admin routes — admin group required.
- spec/API.md §Internal routes — X-Internal-Token required.
- src/api/routers/admin.py _apply_datahub_patch_and_respond — token to Secret first.
- src/api/schemas/admin.py DatahubPeripheralResponse — token masking.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.dependencies import get_db
from src.api.main import app
from src.backend.admin.peripheral_service import DatahubConfigDTO, LangfuseConfigDTO
from src.backend.ingestion.secret_resolver import SecretResolverUnavailable
from src.shared.db.models import PeripheralConfig

from tests.unit.api.conftest import auth_headers

_PERIPHERALS = "/api/v1/admin/peripherals"
_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"
_PERIPHERALS_LF = "/api/v1/admin/peripherals/langfuse"
_INTERNAL_DH = "/internal/admin/peripherals/datahub"
_INTERNAL_LF = "/internal/admin/peripherals/langfuse"
_INTERNAL_TOKEN = "test-internal-secret"

_FAKE_DH_DTO = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
_FAKE_LF_DTO = LangfuseConfigDTO(host="http://langfuse:3000", public_key="pk-test")


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_peripheral_row(name: str) -> MagicMock:
    row = MagicMock(spec=PeripheralConfig)
    row.name = name
    row.updated_at = datetime.now(tz=UTC)
    return row


def _fake_db(dto_for_get=None, updated_at=None) -> tuple:
    """Return (db_mock, override_fn) with scalar_one_or_none returning a mock row."""
    db = AsyncMock()
    row_mock = MagicMock()
    row_mock.updated_at = updated_at or datetime.now(tz=UTC)

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row_mock

    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    async def _gen():
        yield db

    return db, _gen


# ── 1a. Auth: GET /admin/peripherals ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_peripherals_without_token_returns_401(client) -> None:
    """GET /admin/peripherals without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.get(_PERIPHERALS)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_peripherals_non_admin_returns_403(client) -> None:
    """GET /admin/peripherals with non-admin group returns 403.

    spec: API.md §Admin routes — admin group required.
    """
    resp = await client.get(_PERIPHERALS, headers=auth_headers(["de"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_peripherals_da_group_returns_403(client) -> None:
    """GET /admin/peripherals with 'da' group returns 403.

    spec: API.md §Admin routes — admin group required exclusively.
    """
    resp = await client.get(_PERIPHERALS, headers=auth_headers(["da"]))
    assert resp.status_code == 403


# ── 1b. Auth: PATCH /admin/peripherals/datahub ───────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_without_token_returns_401(client) -> None:
    """PATCH /admin/peripherals/datahub without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.patch(_PERIPHERALS_DH, json={"gms_url": "http://gms:8080"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_datahub_non_admin_returns_403(client) -> None:
    """PATCH /admin/peripherals/datahub with non-admin group returns 403.

    spec: API.md §Admin routes — admin group required.
    """
    resp = await client.patch(
        _PERIPHERALS_DH,
        json={"gms_url": "http://gms:8080"},
        headers=auth_headers(["dg"]),
    )
    assert resp.status_code == 403


# ── 1c. Auth: PATCH /internal/admin/peripherals/datahub ──────────────────────


@pytest.mark.asyncio
async def test_internal_patch_datahub_unset_token_returns_503(client) -> None:
    """PATCH /internal/admin/peripherals/datahub when DATASPOKE_INTERNAL_TOKEN blank → 503.

    spec: src/api/auth/internal.py require_internal_token — 503 when settings.internal_token falsy.
    """
    with patch("src.shared.settings.settings.internal_token", ""):
        resp = await client.patch(
            _INTERNAL_DH,
            json={"gms_url": "http://gms:8080"},
            headers={"X-Internal-Token": "any-value"},
        )
    assert resp.status_code == 503
    body = resp.json()
    assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_internal_patch_datahub_without_token_returns_401(client) -> None:
    """PATCH /internal/admin/peripherals/datahub without X-Internal-Token → 401.

    spec: API.md §Internal routes — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(_INTERNAL_DH, json={"gms_url": "http://gms:8080"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_patch_datahub_wrong_token_returns_401(client) -> None:
    """PATCH /internal/admin/peripherals/datahub with wrong X-Internal-Token → 401.

    spec: API.md §Internal routes — constant-time compare; mismatch → 401.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(
            _INTERNAL_DH,
            json={"gms_url": "http://gms:8080"},
            headers={"X-Internal-Token": "wrong-token"},
        )
    assert resp.status_code == 401


# ── 2. GET /admin/peripherals — is_configured predicate ──────────────────────


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_true_when_dto_and_secret_set(client) -> None:
    """is_configured=True only when dto is not None AND secret is set.

    spec: plan/scalable-beaming-hamster.md §is_configured predicate.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(side_effect=[_FAKE_DH_DTO, _FAKE_LF_DTO]),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is True, (
        "is_configured must be True when dto is not None and token is set. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is True


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_dto_none(client) -> None:
    """is_configured=False when dto is None (peripheral not configured in DB).

    spec: plan/scalable-beaming-hamster.md §is_configured predicate — False if dto absent.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto is None. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is False


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_dto_present_but_secret_unset(
    client,
) -> None:
    """is_configured=False when dto is present but secret is unset.

    spec: plan/scalable-beaming-hamster.md §is_configured predicate —
    False if secret unset (row exists but K8s Secret not written).
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(side_effect=[_FAKE_DH_DTO, _FAKE_LF_DTO]),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto present but secret is unset. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is False


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_secret_set_but_dto_none(
    client,
) -> None:
    """is_configured=False when dto is None even though secret IS set.

    is_configured is an AND predicate: dto present AND secret set.
    A K8s Secret without a DB row is not a configured state.

    spec: plan/scalable-beaming-hamster.md §is_configured predicate —
    is_configured = (dto is not None) AND (secret is set).
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            # dto=None for both peripherals (no DB row)
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            # secrets ARE set — this is the asymmetric case
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto is None even if secret is set. "
        "is_configured = (dto not None) AND (secret set); "
        "secret-only does not suffice. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is False, (
        "is_configured must be False for langfuse when dto is None even if secret_key is set."
    )


@pytest.mark.asyncio
async def test_get_datahub_peripheral_is_configured_false_when_secret_set_but_dto_none(
    client,
) -> None:
    """GET /admin/peripherals/datahub: is_configured=False when dto=None and secret IS set.

    This tests the per-peripheral GET endpoint (not the list endpoint).
    is_configured is AND: a K8s Secret without the DB row must not be 'configured'.

    spec: plan/scalable-beaming-hamster.md §is_configured predicate.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "GET /admin/peripherals/datahub must return is_configured=False when dto=None, "
        "even if the K8s Secret is set. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )


@pytest.mark.asyncio
async def test_get_langfuse_peripheral_is_configured_false_when_secret_set_but_dto_none(
    client,
) -> None:
    """GET /admin/peripherals/langfuse: is_configured=False when dto=None and secret IS set.

    spec: plan/scalable-beaming-hamster.md §is_configured predicate.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "GET /admin/peripherals/langfuse must return is_configured=False when dto=None, "
        "even if the K8s Secret is set. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )


# ── 3. GET /admin/peripherals/datahub — response shape + token masking ────────


@pytest.mark.asyncio
async def test_get_datahub_peripheral_token_masked_when_set(client) -> None:
    """GET /admin/peripherals/datahub returns token="********" when set (never plaintext).

    spec: src/api/schemas/admin.py DatahubPeripheralResponse — token masked indicator.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "********", (
        f"token must be masked '********' when set; got {body['token']!r}. "
        "Plaintext must never appear in the response."
    )
    assert "my-datahub-token" not in str(body), (
        "Plaintext token value must never appear anywhere in the response body. "
        "spec: plan/scalable-beaming-hamster.md §API surface — token masking."
    )
    assert body["gms_url"] == "http://gms:8080"
    assert body["kafka_brokers"] == "kafka:9092"
    assert body["is_configured"] is True


@pytest.mark.asyncio
async def test_get_datahub_peripheral_token_empty_when_unset(client) -> None:
    """GET /admin/peripherals/datahub returns token="" when unset.

    spec: src/api/schemas/admin.py DatahubPeripheralResponse — token="" when unset.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "", (
        f"token must be '' when unset; got {body['token']!r}"
    )
    assert body["is_configured"] is False


@pytest.mark.asyncio
async def test_get_datahub_peripheral_empty_fields_when_unconfigured(client) -> None:
    """GET /admin/peripherals/datahub returns empty strings when dto is None.

    spec: src/api/routers/admin.py _datahub_dto_to_response — None dto → all empty.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["gms_url"] == ""
    assert body["kafka_brokers"] == ""
    assert body["token"] == ""
    assert body["is_configured"] is False


# ── 4a. PATCH /admin/peripherals/datahub — token routes to Secret first ───────


@pytest.mark.asyncio
async def test_patch_datahub_token_routes_to_secret_not_db(client) -> None:
    """PATCH /admin/peripherals/datahub with only token calls set_datahub_token; NOT patch_peripheral_config.

    When only token is sent, db_updates is empty so the router calls
    invalidate_peripheral_config_cache + get_peripheral_config (not patch_peripheral_config).
    Token must never reach patch_peripheral_config.

    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond —
    token field is removed from all_updates before DB write; empty db_updates takes the
    invalidate+get path, not the patch_peripheral_config path.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_token = MagicMock()
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO)
    mock_invalidate = MagicMock()
    try:
        with patch("src.api.routers.admin.set_datahub_token", mock_set_token):
            with patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db):
                with patch("src.api.routers.admin.invalidate_peripheral_config_cache", mock_invalidate):
                    with patch("src.api.routers.admin.get_peripheral_config", AsyncMock(return_value=_FAKE_DH_DTO)):
                        with patch("src.api.routers.admin.datahub_token_is_set", return_value=True):
                            resp = await client.patch(
                                _PERIPHERALS_DH,
                                json={"token": "dh-token-value"},
                                headers=auth_headers(["admin"]),
                            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    # set_datahub_token must be called with the plaintext value.
    mock_set_token.assert_called_once_with("dh-token-value")
    # patch_peripheral_config must NOT be called — db_updates is empty
    # when only token is provided (token is not a DB column).
    mock_patch_db.assert_not_called()
    # invalidate must be called so the next read reflects the new secret state.
    mock_invalidate.assert_called_once_with("datahub")

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "********"
    assert "dh-token-value" not in str(body), "Plaintext token must never appear in response."


@pytest.mark.asyncio
async def test_patch_datahub_secret_write_failure_returns_503_and_skips_db(client) -> None:
    """PATCH /admin/peripherals/datahub when Secret write fails → 503; DB NOT updated.

    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond —
    SecretResolverUnavailable → StorageUnavailableError → 503; DB patch skipped.
    """
    app.dependency_overrides[get_db] = lambda: (x for x in [AsyncMock()])
    try:
        with (
            patch(
                "src.api.routers.admin.set_datahub_token",
                side_effect=SecretResolverUnavailable("out of cluster"),
            ),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(),
            ) as mock_patch_db,
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"token": "sk-test", "gms_url": "http://gms:8080"},
                headers=auth_headers(["admin"]),
            )
            # DB must NOT be updated when Secret write failed.
            mock_patch_db.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 503, (
        f"SecretResolverUnavailable must map to 503; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_datahub_empty_token_clears_secret(client) -> None:
    """PATCH /admin/peripherals/datahub with token="" calls set_datahub_token("") to clear.

    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond —
    explicit "" clears the secret; None (omitted) leaves it unchanged.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_token = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_datahub_token", mock_set_token),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", MagicMock()),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"token": ""},
                headers=auth_headers(["admin"]),
            )
            mock_set_token.assert_called_once_with("")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == ""


@pytest.mark.asyncio
async def test_patch_datahub_omitting_token_does_not_touch_secret(client) -> None:
    """PATCH /admin/peripherals/datahub without token field does not call set_datahub_token.

    Omitting token means "leave unchanged" — the Secret write must be skipped.

    spec: src/api/schemas/admin.py DatahubPeripheralPatchRequest — token default None.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_token = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_datahub_token", mock_set_token),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"gms_url": "http://new-gms:8080"},
                headers=auth_headers(["admin"]),
            )
            mock_set_token.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_datahub_gms_url_and_kafka_brokers_written_to_db(client) -> None:
    """PATCH /admin/peripherals/datahub with gms_url and kafka_brokers calls patch_peripheral_config.

    Non-secret fields (gms_url, kafka_brokers) are written to the DB.

    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond —
    db_updates = {k: v for k, v in all_updates.items() if v is not None}.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ) as mock_patch_db,
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"gms_url": "http://new-gms:8080", "kafka_brokers": "kafka-new:9092"},
                headers=auth_headers(["admin"]),
            )
            mock_patch_db.assert_called_once()
            call_args = mock_patch_db.call_args
            assert call_args is not None
            # patch_peripheral_config is called as (db, "datahub", gms_url=..., kafka_brokers=...)
            assert len(call_args.args) >= 2 and call_args.args[1] == "datahub", (
                f"patch_peripheral_config must be called with 'datahub' as second arg; "
                f"got args={call_args.args!r}, kwargs={call_args.kwargs!r}. "
                "spec: src/api/routers/admin.py _apply_datahub_patch_and_respond."
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


# ── 5. GET /admin/peripherals/langfuse ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_langfuse_peripheral_secret_key_masked_when_set(client) -> None:
    """GET /admin/peripherals/langfuse returns secret_key="********" when set.

    spec: src/api/schemas/admin.py LangfusePeripheralResponse — secret_key masked.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["secret_key"] == "********", (
        f"secret_key must be masked '********' when set; got {body['secret_key']!r}"
    )
    assert "sk-langfuse-key" not in str(body), (
        "Plaintext secret_key value must never appear anywhere in the response body. "
        "spec: plan/scalable-beaming-hamster.md §API surface — secret masking."
    )
    assert body["host"] == "http://langfuse:3000"
    assert body["public_key"] == "pk-test"
    assert body["is_configured"] is True


@pytest.mark.asyncio
async def test_get_langfuse_peripheral_empty_fields_when_unconfigured(client) -> None:
    """GET /admin/peripherals/langfuse returns empty fields when dto is None.

    spec: src/api/routers/admin.py _langfuse_dto_to_response — None dto → all empty.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers(["admin"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == ""
    assert body["public_key"] == ""
    assert body["secret_key"] == ""
    assert body["is_configured"] is False


# ── 5a. PATCH /admin/peripherals/langfuse — secret routes to Secret first ─────


@pytest.mark.asyncio
async def test_patch_langfuse_secret_key_routes_to_secret_not_db(client) -> None:
    """PATCH /admin/peripherals/langfuse with secret_key calls set_langfuse_secret_key, NOT DB.

    spec: src/api/routers/admin.py _apply_langfuse_patch_and_respond — secret_key to Secret.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_secret = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_langfuse_secret_key", mock_set_secret),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ) as mock_patch_db,
            # token-only PATCH → db_updates empty → code takes invalidate+get path,
            # so get_peripheral_config must be mocked to avoid hitting the real DB.
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", MagicMock()),
        ):
            resp = await client.patch(
                _PERIPHERALS_LF,
                json={"secret_key": "lf-secret-value"},
                headers=auth_headers(["admin"]),
            )
            mock_set_secret.assert_called_once_with("lf-secret-value")
            call_kwargs = mock_patch_db.call_args
            if call_kwargs is not None:
                _, kwargs = call_kwargs
                assert "secret_key" not in kwargs, (
                    "secret_key must never be passed to patch_peripheral_config."
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["secret_key"] == "********"
    assert "lf-secret-value" not in str(body)


@pytest.mark.asyncio
async def test_patch_langfuse_secret_write_failure_returns_503(client) -> None:
    """PATCH /admin/peripherals/langfuse when Secret write fails → 503; DB NOT updated.

    spec: src/api/routers/admin.py _apply_langfuse_patch_and_respond —
    SecretResolverUnavailable → StorageUnavailableError → 503.
    """
    app.dependency_overrides[get_db] = lambda: (x for x in [AsyncMock()])
    try:
        with (
            patch(
                "src.api.routers.admin.set_langfuse_secret_key",
                side_effect=SecretResolverUnavailable("out of cluster"),
            ),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(),
            ) as mock_patch_db,
        ):
            resp = await client.patch(
                _PERIPHERALS_LF,
                json={"secret_key": "lf-secret", "host": "http://langfuse:3000"},
                headers=auth_headers(["admin"]),
            )
            mock_patch_db.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_patch_langfuse_empty_secret_key_clears_secret(client) -> None:
    """PATCH /admin/peripherals/langfuse with secret_key="" calls set_langfuse_secret_key("").

    spec: src/api/routers/admin.py _apply_langfuse_patch_and_respond — explicit "" clears.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_secret = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_langfuse_secret_key", mock_set_secret),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=False),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", MagicMock()),
        ):
            resp = await client.patch(
                _PERIPHERALS_LF,
                json={"secret_key": ""},
                headers=auth_headers(["admin"]),
            )
            mock_set_secret.assert_called_once_with("")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["secret_key"] == ""


# ── 6. PATCH /internal/admin/peripherals/datahub — valid token → 200 ─────────


@pytest.mark.asyncio
async def test_internal_patch_datahub_valid_token_returns_200(client) -> None:
    """PATCH /internal/admin/peripherals/datahub with correct X-Internal-Token → 200.

    spec: API.md §Internal routes — valid token grants access.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _INTERNAL_DH,
                json={"gms_url": "http://gms:8080"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["gms_url"] == "http://gms:8080"


# ── 7. PATCH /internal/admin/peripherals/langfuse — valid token → 200 ────────


@pytest.mark.asyncio
async def test_internal_patch_langfuse_valid_token_returns_200(client) -> None:
    """PATCH /internal/admin/peripherals/langfuse with correct X-Internal-Token → 200.

    spec: API.md §Internal routes — valid token grants access.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.patch(
                _INTERNAL_LF,
                json={"host": "http://langfuse:3000"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "http://langfuse:3000"


# ── F15. PATCH /internal route also routes token through K8s Secret ───────────


@pytest.mark.asyncio
async def test_internal_patch_datahub_token_calls_set_datahub_token(client) -> None:
    """PATCH /internal/admin/peripherals/datahub with token routes token to K8s Secret.

    The internal route must call set_datahub_token just as the admin route does.
    Proves the internal endpoint also routes the secret through the K8s Secret.

    spec: plan/scalable-beaming-hamster.md §API surface — all PATCH routes write
    token to K8s Secret, never to DB.
    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond shared helper.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_token = MagicMock()
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch("src.api.routers.admin.set_datahub_token", mock_set_token),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", MagicMock()),
        ):
            resp = await client.patch(
                _INTERNAL_DH,
                json={"token": "x"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    mock_set_token.assert_called_once_with("x"), (
        "The internal PATCH /datahub route must call set_datahub_token('x'). "
        "spec: plan/scalable-beaming-hamster.md §API surface."
    )


@pytest.mark.asyncio
async def test_internal_patch_langfuse_token_calls_set_langfuse_secret_key(client) -> None:
    """PATCH /internal/admin/peripherals/langfuse with secret_key routes to K8s Secret.

    The internal route must call set_langfuse_secret_key just as the admin route does.

    spec: plan/scalable-beaming-hamster.md §API surface — all PATCH routes write
    secret_key to K8s Secret, never to DB.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_secret = MagicMock()
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch("src.api.routers.admin.set_langfuse_secret_key", mock_set_secret),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", MagicMock()),
        ):
            resp = await client.patch(
                _INTERNAL_LF,
                json={"secret_key": "x"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    mock_set_secret.assert_called_once_with("x"), (
        "The internal PATCH /langfuse route must call set_langfuse_secret_key('x'). "
        "spec: plan/scalable-beaming-hamster.md §API surface."
    )


# ── F16. Empty PATCH body does not create a row ───────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_empty_body_does_not_create_row(client) -> None:
    """PATCH /admin/peripherals/datahub with {} body: 200, no DB write, no Secret write.

    An empty body contains no DB fields and no token, so neither
    patch_peripheral_config nor set_datahub_token should be called.

    spec: plan/scalable-beaming-hamster.md §Backend — F6: empty partial no-op.
    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond — skips Secret
    write when token is None; skips DB write when db_updates is empty.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO)
    mock_set_token = MagicMock()
    mock_invalidate = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_datahub_token", mock_set_token),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", mock_invalidate),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={},
                headers=auth_headers(["admin"]),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, (
        f"Empty PATCH body must return 200; got {resp.status_code}: {resp.text}. "
        "spec: plan/scalable-beaming-hamster.md §Backend — F6: empty partial no-op."
    )
    mock_patch_db.assert_not_called(), (
        "patch_peripheral_config must NOT be called for an empty PATCH body. "
        "spec: plan/scalable-beaming-hamster.md §Backend — no-op empty partial."
    )
    mock_set_token.assert_not_called(), (
        "set_datahub_token must NOT be called when token field is absent. "
        "spec: plan/scalable-beaming-hamster.md §Backend — token omitted = leave unchanged."
    )
