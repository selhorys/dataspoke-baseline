"""Unit tests for admin peripheral routes.

Routes under test:
  GET  /api/v1/admin/peripherals                — requires Admin role
  GET  /api/v1/admin/peripherals/datahub        — requires Admin role
  PATCH /api/v1/admin/peripherals/datahub       — requires Admin role
  GET  /api/v1/admin/peripherals/langfuse       — requires Admin role
  PATCH /api/v1/admin/peripherals/langfuse      — requires Admin role
  PATCH /internal/admin/peripherals/datahub     — requires X-Internal-Token
  PATCH /internal/admin/peripherals/langfuse    — requires X-Internal-Token

Concerns covered:

1. Auth gates:
   - GET  /admin/peripherals without JWT → 401
   - GET  /admin/peripherals with non-Admin role → 403
   - PATCH /admin/peripherals/datahub without JWT → 401
   - PATCH /admin/peripherals/datahub with non-Admin role → 403
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
- spec/API.md §Admin (/admin) — is_configured predicate (DB row AND Secret).
- spec/API.md §Access Control — Admin role required for /admin/*.
- spec/API.md §Internal Admin (/internal/admin) — X-Internal-Token required.
- src/api/routers/admin.py _apply_datahub_patch_and_respond — token to Secret first.
- src/api/schemas/admin.py DatahubPeripheralResponse — token masking.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.dependencies import get_db
from src.api.main import app
from src.backend.admin.peripheral_service import (
    DatahubConfigDTO,
    LangfuseConfigDTO,
    SmtpConfigDTO,
)
from src.shared.db.models import PeripheralConfig
from src.shared.secrets import SecretResolverUnavailable
from tests.unit.api.conftest import _make_mock_user, auth_headers
from tests.unit.conftest import route_db_execute

_PERIPHERALS = "/api/v1/admin/peripherals"
_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"
_PERIPHERALS_LF = "/api/v1/admin/peripherals/langfuse"
_PERIPHERALS_SMTP = "/api/v1/admin/peripherals/smtp"
_INTERNAL_DH = "/internal/admin/peripherals/datahub"
_INTERNAL_LF = "/internal/admin/peripherals/langfuse"
_INTERNAL_SMTP = "/internal/admin/peripherals/smtp"
_INTERNAL_TOKEN = "test-internal-secret"

_FAKE_DH_DTO = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
_FAKE_LF_DTO = LangfuseConfigDTO(host="http://langfuse:3000", public_key="pk-test")

# SMTP DTO with the non-secret connection settings that make a peripheral configurable:
# host AND from_address must both be non-empty for is_configured (with the password set).
_FAKE_SMTP_DTO = SmtpConfigDTO(
    host="smtp.example.com",
    port=587,
    username="mailer@example.com",
    from_address="noreply@example.com",
    use_tls=True,
)

# DTOs with the non-secret connection settings populated.
_FAKE_DH_DTO_FULL = DatahubConfigDTO(
    gms_url="http://gms:8080",
    kafka_brokers="kafka:9092",
    service_corpuser_urn="urn:li:corpuser:imazon-svc",
    default_env="PROD",
    # Differs from gms_url in host, port, AND scheme — the browser-facing UI URL
    # cannot be derived from the GMS service endpoint.
    frontend_url="https://datahub.imazon.example.com",
)
_FAKE_LF_DTO_FULL = LangfuseConfigDTO(
    host="http://langfuse:3000",
    public_key="pk-test",
    project_id="imazon-metadata",
    environment_tag="production",
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_peripheral_row(name: str) -> MagicMock:
    row = MagicMock(spec=PeripheralConfig)
    row.name = name
    row.updated_at = datetime.now(tz=UTC)
    return row


def _fake_db(dto_for_get=None, updated_at=None) -> tuple:
    """Return (db_mock, override_fn) with scalar_one_or_none returning a mock row.

    The first execute() call satisfies require_authenticated's user lookup
    (returns an Admin User mock).  Subsequent calls return a PeripheralConfig
    row so that service/route logic sees expected data.
    """
    db = AsyncMock()
    row_mock = MagicMock()
    row_mock.updated_at = updated_at or datetime.now(tz=UTC)

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user(role="Admin")

    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = row_mock

    # The auth user-lookup hits the users table; the PeripheralConfig reads are routed
    # by their own table, so an added/reordered config query keeps its correct result.
    route_db_execute(db, [("users", auth_result)], default=row_result)
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
    """GET /admin/peripherals with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.get(_PERIPHERALS, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


@pytest.mark.asyncio
async def test_get_peripherals_editor_role_returns_403(client) -> None:
    """GET /admin/peripherals with Editor role returns 403.

    spec: API.md §Admin routes — Admin role required exclusively.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext

    editor_ctx = AuthContext(user=_make_mock_user(role="Editor"), effective_role="Editor")
    app.dependency_overrides[require_authenticated] = lambda: editor_ctx
    try:
        resp = await client.get(_PERIPHERALS, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


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
    """PATCH /admin/peripherals/datahub with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.patch(
            _PERIPHERALS_DH,
            json={"gms_url": "http://gms:8080"},
            headers=auth_headers(),
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


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

    spec: API.md §Internal Admin (/internal/admin) — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(_INTERNAL_DH, json={"gms_url": "http://gms:8080"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_patch_datahub_wrong_token_returns_401(client) -> None:
    """PATCH /internal/admin/peripherals/datahub with wrong X-Internal-Token → 401.

    spec: API.md §Internal Admin (/internal/admin) — constant-time compare; mismatch → 401.
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

    spec: spec/API.md §Admin (/admin) — is_configured predicate.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(side_effect=[_FAKE_DH_DTO, _FAKE_LF_DTO, None]),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is True, (
        "is_configured must be True when dto is not None and token is set. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is True


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_dto_none(client) -> None:
    """is_configured=False when dto is None (peripheral not configured in DB).

    spec: spec/API.md §Admin (/admin) — is_configured predicate — False if dto absent.
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
            resp = await client.get(_PERIPHERALS, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto is None. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is False


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_dto_present_but_secret_unset(
    client,
) -> None:
    """is_configured=False when dto is present but secret is unset.

    spec: spec/API.md §Admin (/admin) — is_configured predicate —
    False if secret unset (row exists but K8s Secret not written).
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(side_effect=[_FAKE_DH_DTO, _FAKE_LF_DTO, None]),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=False),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto present but secret is unset. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
    )
    assert body["langfuse"]["is_configured"] is False


@pytest.mark.asyncio
async def test_get_peripherals_is_configured_false_when_secret_set_but_dto_none(
    client,
) -> None:
    """is_configured=False when dto is None even though secret IS set.

    is_configured is an AND predicate: dto present AND secret set.
    A K8s Secret without a DB row is not a configured state.

    spec: spec/API.md §Admin (/admin) — is_configured predicate —
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
            resp = await client.get(_PERIPHERALS, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["datahub"]["is_configured"] is False, (
        "is_configured must be False when dto is None even if secret is set. "
        "is_configured = (dto not None) AND (secret set); "
        "secret-only does not suffice. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
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

    spec: spec/API.md §Admin (/admin) — is_configured predicate.
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
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "GET /admin/peripherals/datahub must return is_configured=False when dto=None, "
        "even if the K8s Secret is set. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
    )


@pytest.mark.asyncio
async def test_get_langfuse_peripheral_is_configured_false_when_secret_set_but_dto_none(
    client,
) -> None:
    """GET /admin/peripherals/langfuse: is_configured=False when dto=None and secret IS set.

    spec: spec/API.md §Admin (/admin) — is_configured predicate.
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
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "GET /admin/peripherals/langfuse must return is_configured=False when dto=None, "
        "even if the K8s Secret is set. "
        "spec: spec/API.md §Admin (/admin) — is_configured predicate."
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
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    # The masking contract: a configured token is reported as "********", never the
    # plaintext. The handler reads only the *_is_set boolean and emits the mask, so this
    # positive assertion is the direct guard against a regression that serialized the
    # real value. spec: spec/API.md §Admin (/admin) — token masking.
    assert body["token"] == "********", (
        f"token must be masked '********' when set; got {body['token']!r}."
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
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
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
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
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
    """PATCH /admin/peripherals/datahub with only token calls set_datahub_token, NOT the DB.

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
        with (
            patch("src.api.routers.admin.set_datahub_token", mock_set_token),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.invalidate_peripheral_config_cache", mock_invalidate),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"token": "dh-token-value"},
                headers=auth_headers(),
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
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
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
                headers=auth_headers(),
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
                headers=auth_headers(),
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
                headers=auth_headers(),
            )
            mock_set_token.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_datahub_gms_url_and_kafka_brokers_written_to_db(client) -> None:
    """PATCH /admin/peripherals/datahub with gms_url + kafka_brokers calls the DB patch.

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
                headers=auth_headers(),
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
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    # The masking contract: a configured secret_key is reported as "********", never the
    # plaintext. The handler reads only the *_is_set boolean and emits the mask, so this
    # positive assertion is the direct guard against a regression that serialized the
    # real value. spec: spec/API.md §Admin (/admin) — secret masking.
    assert body["secret_key"] == "********", (
        f"secret_key must be masked '********' when set; got {body['secret_key']!r}"
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
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers())
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
                headers=auth_headers(),
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
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
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
                headers=auth_headers(),
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
                headers=auth_headers(),
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

    spec: API.md §Internal Admin (/internal/admin) — valid token grants access.
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

    spec: API.md §Internal Admin (/internal/admin) — valid token grants access.
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

    spec: spec/API.md §Admin (/admin) — all PATCH routes write
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
    # The internal PATCH /datahub route must call set_datahub_token('x').
    # spec: spec/API.md §Admin (/admin).
    mock_set_token.assert_called_once_with("x")


@pytest.mark.asyncio
async def test_internal_patch_langfuse_token_calls_set_langfuse_secret_key(client) -> None:
    """PATCH /internal/admin/peripherals/langfuse with secret_key routes to K8s Secret.

    The internal route must call set_langfuse_secret_key just as the admin route does.

    spec: spec/API.md §Admin (/admin) — all PATCH routes write
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
    # The internal PATCH /langfuse route must call set_langfuse_secret_key('x').
    # spec: spec/API.md §Admin (/admin).
    mock_set_secret.assert_called_once_with("x")


# ── F16. Empty PATCH body does not create a row ───────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_empty_body_does_not_create_row(client) -> None:
    """PATCH /admin/peripherals/datahub with {} body: 200, no DB write, no Secret write.

    An empty body contains no DB fields and no token, so neither
    patch_peripheral_config nor set_datahub_token should be called.

    spec: spec/API.md §Admin (/admin) — F6: empty partial no-op.
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
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, (
        f"Empty PATCH body must return 200; got {resp.status_code}: {resp.text}. "
        "spec: spec/API.md §Admin (/admin) — F6: empty partial no-op."
    )
    # patch_peripheral_config must NOT be called for an empty PATCH body.
    # spec: spec/API.md §Admin (/admin) — no-op empty partial.
    mock_patch_db.assert_not_called()
    # set_datahub_token must NOT be called when token field is absent.
    # spec: spec/API.md §Admin (/admin) — token omitted = leave unchanged.
    mock_set_token.assert_not_called()


# ── New non-secret connection settings: round-trip through PATCH / GET ─────────
#
# spec: spec/API.md §/admin/peripherals/datahub + /langfuse — frontend_url,
#   service_corpuser_urn, default_env, project_id, environment_tag are non-secret
#   and returned plain (never masked); unset rows read back factory defaults.
# spec: src/api/schemas/admin.py Datahub/LangfusePeripheral{Response,PatchRequest}.


@pytest.mark.asyncio
async def test_get_datahub_returns_non_secret_fields_plain(client) -> None:
    """GET returns frontend_url, service_corpuser_urn, and default_env plain (not masked).

    spec: spec/API.md §Admin — GET /admin/peripherals/datahub: "`frontend_url`
        (the browser-facing DataHub UI URL, distinct from the `gms_url` service
        endpoint), `service_corpuser_urn`, and `default_env` are non-secret and
        returned plain".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_FULL),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["service_corpuser_urn"] == "urn:li:corpuser:imazon-svc", (
        "service_corpuser_urn must be returned plain. "
        "spec: spec/API.md §/admin/peripherals/datahub."
    )
    assert body["default_env"] == "PROD", (
        "default_env must be returned plain. spec: spec/API.md §/admin/peripherals/datahub."
    )
    assert body["frontend_url"] == "https://datahub.imazon.example.com", (
        "frontend_url must be returned plain. spec: spec/API.md §Admin — "
        "GET /admin/peripherals/datahub returns frontend_url non-secret and plain."
    )
    assert body["frontend_url"] != body["gms_url"], (
        "frontend_url is the browser-facing UI URL and must not be conflated with "
        "the gms_url service endpoint."
    )
    # Plain (non-secret) — never masked. The frontend_url check is load-bearing
    # beyond documentation: a mapper regression that masked it would otherwise be
    # invisible here AND would make the spot test's snapshot/restore round-trip
    # write "********" into peripheral_config while still comparing equal.
    assert body["frontend_url"] != "********", (
        "frontend_url must never be masked — it is non-secret."
    )
    assert body["service_corpuser_urn"] != "********"
    assert body["default_env"] != "********"


@pytest.mark.asyncio
async def test_get_datahub_unconfigured_reads_back_factory_defaults(client) -> None:
    """GET /admin/peripherals/datahub on an unconfigured row reads back factory defaults.

    spec: spec/API.md §/admin/peripherals/datahub — unset rows read back factory
        defaults (service_corpuser_urn → urn:li:corpuser:dataspoke, default_env → DEV).
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
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["service_corpuser_urn"] == "urn:li:corpuser:dataspoke", (
        "Unset service_corpuser_urn must read back the factory default. "
        "spec: spec/API.md §/admin/peripherals/datahub."
    )
    assert body["default_env"] == "DEV", (
        "Unset default_env must read back the factory default 'DEV'. "
        "spec: spec/API.md §/admin/peripherals/datahub."
    )
    assert body["frontend_url"] == "", (
        "An unconfigured DataHub peripheral has no browser-facing URL and none is "
        "derivable from gms_url, so frontend_url must read back ''. "
        "spec: spec/API.md §Data Resource — an unconfigured peripheral yields ''."
    )


@pytest.mark.asyncio
async def test_patch_datahub_new_fields_written_to_db_and_reflected(client) -> None:
    """PATCH /admin/peripherals/datahub with the new fields writes them to the DB and echoes them.

    The non-secret service_corpuser_urn + default_env must be passed to
    patch_peripheral_config and reflected in the response.

    spec: spec/API.md §/admin/peripherals/datahub — non-secret fields stored in
        peripheral_config.settings JSONB.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_FULL),
            ) as mock_patch_db,
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={
                    "service_corpuser_urn": "urn:li:corpuser:imazon-svc",
                    "default_env": "PROD",
                },
                headers=auth_headers(),
            )
            mock_patch_db.assert_called_once()
            _, kwargs = mock_patch_db.call_args
            assert kwargs.get("service_corpuser_urn") == "urn:li:corpuser:imazon-svc", (
                "service_corpuser_urn must be forwarded to patch_peripheral_config. "
                f"got kwargs={kwargs!r}."
            )
            assert kwargs.get("default_env") == "PROD", (
                "default_env must be forwarded to patch_peripheral_config. "
                f"got kwargs={kwargs!r}."
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["service_corpuser_urn"] == "urn:li:corpuser:imazon-svc"
    assert body["default_env"] == "PROD"


@pytest.mark.asyncio
async def test_get_langfuse_returns_project_id_and_environment_tag_plain(client) -> None:
    """GET /admin/peripherals/langfuse returns project_id + environment_tag plain (not masked).

    spec: spec/API.md §/admin/peripherals/langfuse — non-secret settings returned
        plain alongside the masked secret_key.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO_FULL),
            ),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_LF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == "imazon-metadata", (
        "project_id must be returned plain. spec: spec/API.md §/admin/peripherals/langfuse."
    )
    assert body["environment_tag"] == "production", (
        "environment_tag must be returned plain. "
        "spec: spec/API.md §/admin/peripherals/langfuse."
    )
    assert body["project_id"] != "********"
    assert body["environment_tag"] != "********"


@pytest.mark.asyncio
async def test_patch_langfuse_new_fields_written_to_db_and_reflected(client) -> None:
    """PATCH /admin/peripherals/langfuse with project_id + environment_tag writes + echoes them.

    spec: spec/API.md §/admin/peripherals/langfuse — non-secret fields stored in
        peripheral_config.settings JSONB and surfaced to LLM tracing.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_LF_DTO_FULL),
            ) as mock_patch_db,
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_LF,
                json={"project_id": "imazon-metadata", "environment_tag": "production"},
                headers=auth_headers(),
            )
            mock_patch_db.assert_called_once()
            _, kwargs = mock_patch_db.call_args
            assert kwargs.get("project_id") == "imazon-metadata", (
                f"project_id must be forwarded to patch_peripheral_config; got {kwargs!r}."
            )
            assert kwargs.get("environment_tag") == "production", (
                f"environment_tag must be forwarded to patch_peripheral_config; got {kwargs!r}."
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == "imazon-metadata"
    assert body["environment_tag"] == "production"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    ["not-a-urn", "urn:li:corpuser:has space", "urn:li:corpuser:a,b", "urn:li:dataset:x"],
)
async def test_patch_datahub_rejects_malformed_corpuser_urn(client, bad_value) -> None:
    """PATCH rejects a malformed service_corpuser_urn with 422 before it reaches the DB.

    The actor URN is stamped verbatim on emitted DataHub audit aspects, so a
    non-corpuser / structurally-invalid value must be refused at the API boundary.

    spec: spec/API.md §/admin/peripherals/datahub — service_corpuser_urn names the
        corpuser actor DataSpoke stamps on emitted audit aspects.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with patch(
            "src.api.routers.admin.patch_peripheral_config",
            AsyncMock(return_value=_FAKE_DH_DTO_FULL),
        ) as mock_patch_db:
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"service_corpuser_urn": bad_value},
                headers=auth_headers(),
            )
            assert resp.status_code == 422, f"expected 422 for {bad_value!r}"
            mock_patch_db.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["1prod", "has space", "a,b", "(x)"])
async def test_patch_datahub_rejects_malformed_default_env(client, bad_value) -> None:
    """PATCH rejects a malformed default_env with 422 before it reaches the DB.

    default_env becomes the DataHub fabric of emitted dataset URNs when a recipe
    omits env; a structurally-invalid value must be refused at the API boundary
    rather than failing at emit time.

    spec: spec/API.md §/admin/peripherals/datahub — default_env is the fabric/env
        applied when an ingestion recipe omits env.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with patch(
            "src.api.routers.admin.patch_peripheral_config",
            AsyncMock(return_value=_FAKE_DH_DTO_FULL),
        ) as mock_patch_db:
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"default_env": bad_value},
                headers=auth_headers(),
            )
            assert resp.status_code == 422, f"expected 422 for {bad_value!r}"
            mock_patch_db.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_patch_datahub_accepts_empty_strings_to_clear_new_fields(client) -> None:
    """PATCH accepts "" for the new non-secret fields (clears back to factory default).

    An explicit empty string is a valid "reset" signal and must pass validation so
    the read-back falls through to the factory default; the validators reject only
    malformed *non-empty* values.

    spec: spec/API.md §/admin/peripherals/datahub — unset rows read back factory
        defaults (service_corpuser_urn → urn:li:corpuser:dataspoke, default_env → DEV).
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_FULL),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"service_corpuser_urn": "", "default_env": ""},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


# ── SMTP peripheral ───────────────────────────────────────────────────────────
#
# spec: spec/API.md §Admin (/admin) — GET /admin/peripherals/smtp returns
#   {host, port, username, from_address, use_tls, password, is_configured,
#   updated_at}; `password` is masked ("" unset, "********" set).
# spec: spec/API.md §Admin (/admin) — `is_configured` is a logical AND (config
#   row present AND the associated K8s Secret set); PATCH routes the secret field
#   (password) to the K8s Secret first, skipping the DB write on failure (503).
# spec: spec/feature/BACKEND.md §Notifications / §SMTP Peripheral — SMTP is
#   unconfigured when there is no row, host/from_address empty, or password unset.


# ── SMTP auth gates ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_smtp_without_token_returns_401(client) -> None:
    """GET /admin/peripherals/smtp without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.get(_PERIPHERALS_SMTP)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_smtp_non_admin_returns_403(client) -> None:
    """GET /admin/peripherals/smtp with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


@pytest.mark.asyncio
async def test_patch_smtp_without_token_returns_401(client) -> None:
    """PATCH /admin/peripherals/smtp without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.patch(_PERIPHERALS_SMTP, json={"host": "smtp.example.com"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_smtp_non_admin_returns_403(client) -> None:
    """PATCH /admin/peripherals/smtp with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.patch(
            _PERIPHERALS_SMTP,
            json={"host": "smtp.example.com"},
            headers=auth_headers(),
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


# ── SMTP GET — password masking ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_smtp_password_masked_when_set(client) -> None:
    """GET /admin/peripherals/smtp returns password="********" when set (never plaintext).

    The handler reads only smtp_password_is_set() and emits the mask, so the
    positive == "********" assertion is the direct guard against a regression that
    serialized the real password.

    spec: spec/API.md §Admin (/admin) — GET /admin/peripherals/smtp `password` is
        masked ("" unset, "********" set).
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["password"] == "********", (
        f"password must be masked '********' when set; got {body['password']!r}."
    )
    # Non-secret connection settings are returned plain.
    assert body["host"] == "smtp.example.com"
    assert body["port"] == 587
    assert body["username"] == "mailer@example.com"
    assert body["from_address"] == "noreply@example.com"
    assert body["use_tls"] is True


@pytest.mark.asyncio
async def test_get_smtp_password_empty_when_unset(client) -> None:
    """GET /admin/peripherals/smtp returns password="" when the Secret is unset.

    spec: spec/API.md §Admin (/admin) — `password` is "" when unset.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["password"] == "", (
        f"password must be '' when unset; got {body['password']!r}"
    )


@pytest.mark.asyncio
async def test_get_smtp_empty_fields_when_unconfigured(client) -> None:
    """GET /admin/peripherals/smtp returns empty/default fields when dto is None.

    spec: src/api/routers/admin.py _smtp_dto_to_response — None dto → empty fields,
        is_configured False.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=None),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == ""
    assert body["from_address"] == ""
    assert body["password"] == ""
    assert body["is_configured"] is False


# ── SMTP is_configured predicate: host AND from_address AND password set ───────


@pytest.mark.asyncio
async def test_get_smtp_is_configured_true_when_host_from_and_password_all_set(client) -> None:
    """is_configured=True only when host AND from_address AND password are all set.

    spec: spec/feature/BACKEND.md §Notifications — SMTP is unconfigured when there
        is no row, host/from_address empty, or password unset; the configured state
        is the conjunction of all three.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is True, (
        "is_configured must be True when host, from_address, and password are all set. "
        "spec: spec/feature/BACKEND.md §Notifications."
    )


@pytest.mark.asyncio
async def test_get_smtp_is_configured_false_when_password_unset(client) -> None:
    """is_configured=False when host+from_address are set but the password is unset.

    spec: spec/feature/BACKEND.md §Notifications — password unset ⇒ SMTP unconfigured.
    spec: spec/API.md §Admin (/admin) — is_configured is an AND over the Secret.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=False),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "is_configured must be False when the password Secret is unset even though "
        "host and from_address are present. spec: spec/feature/BACKEND.md §Notifications."
    )


@pytest.mark.asyncio
async def test_get_smtp_is_configured_false_when_from_address_empty(client) -> None:
    """is_configured=False when from_address is empty even though host+password are set.

    Isolates the from_address conjunct: with the password set and host present, an
    empty from_address alone must still read back unconfigured.

    spec: spec/feature/BACKEND.md §Notifications — host/from_address empty ⇒
        SMTP unconfigured.
    """
    dto_no_from = SmtpConfigDTO(
        host="smtp.example.com",
        port=587,
        username="mailer@example.com",
        from_address="",
        use_tls=True,
    )
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=dto_no_from),
            ),
            # password IS set — isolates the from_address conjunct as the sole cause.
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "is_configured must be False when from_address is empty, even with host and "
        "password set. spec: spec/feature/BACKEND.md §Notifications."
    )


@pytest.mark.asyncio
async def test_get_smtp_is_configured_false_when_host_empty(client) -> None:
    """is_configured=False when host is empty even though from_address+password are set.

    Isolates the host conjunct: with the password set and from_address present, an
    empty host alone must still read back unconfigured. Without this case, dropping
    the host term from the impl predicate would pass the other conjunct tests.

    spec: spec/feature/BACKEND.md §Notifications — host/from_address empty ⇒
        SMTP unconfigured.
    """
    dto_no_host = SmtpConfigDTO(
        host="",
        port=587,
        username="mailer@example.com",
        from_address="noreply@example.com",
        use_tls=True,
    )
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=dto_no_host),
            ),
            # password IS set — isolates the host conjunct as the sole cause.
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_SMTP, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_configured"] is False, (
        "is_configured must be False when host is empty, even with from_address and "
        "password set. spec: spec/feature/BACKEND.md §Notifications."
    )


@pytest.mark.asyncio
async def test_get_peripherals_status_smtp_is_configured(client) -> None:
    """GET /admin/peripherals reports smtp.is_configured with the same AND predicate.

    The status-overview endpoint must fold host + from_address + password_set into
    the smtp is_configured flag, matching the per-peripheral GET.

    spec: spec/API.md §Admin (/admin) — GET /admin/peripherals returns
        {..., smtp: {is_configured}}.
    spec: spec/feature/BACKEND.md §Notifications — SMTP configured = host AND
        from_address AND password set.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(side_effect=[_FAKE_DH_DTO, _FAKE_LF_DTO, _FAKE_SMTP_DTO]),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch("src.api.routers.admin.langfuse_secret_key_is_set", return_value=True),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["smtp"]["is_configured"] is True, (
        "smtp.is_configured must be True when host, from_address, and password are set. "
        "spec: spec/feature/BACKEND.md §Notifications."
    )


# ── SMTP PATCH — password routed to Secret, never to DB ────────────────────────


@pytest.mark.asyncio
async def test_patch_smtp_password_routes_to_secret_not_db(client) -> None:
    """PATCH /admin/peripherals/smtp with only password calls set_smtp_password, NOT the DB.

    When only password is sent, db_updates is empty so the router calls
    invalidate_smtp_password_cache + get_peripheral_config (not patch_peripheral_config).
    The plaintext password must never reach patch_peripheral_config.

    spec: spec/API.md §Admin (/admin) — PATCH routes the secret field (password) to
        the K8s Secret, never to the DB.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_pw = MagicMock()
    mock_patch_db = AsyncMock(return_value=_FAKE_SMTP_DTO)
    mock_invalidate = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_smtp_password", mock_set_pw),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.invalidate_smtp_password_cache", mock_invalidate),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_SMTP,
                json={"password": "smtp-pw-value"},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    mock_set_pw.assert_called_once_with("smtp-pw-value")
    mock_patch_db.assert_not_called()
    mock_invalidate.assert_called_once()

    assert resp.status_code == 200
    body = resp.json()
    assert body["password"] == "********"
    assert "smtp-pw-value" not in str(body), "Plaintext password must never appear in response."


@pytest.mark.asyncio
async def test_patch_smtp_secret_write_failure_returns_503_and_skips_db(client) -> None:
    """PATCH /admin/peripherals/smtp when Secret write fails → 503; DB NOT updated.

    spec: spec/API.md §Admin (/admin) — the DB write is skipped if the Secret write
        fails (503).
    spec: src/api/routers/admin.py _apply_smtp_patch_and_respond —
        SecretResolverUnavailable → StorageUnavailableError → 503.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.set_smtp_password",
                side_effect=SecretResolverUnavailable("out of cluster"),
            ),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(),
            ) as mock_patch_db,
        ):
            resp = await client.patch(
                _PERIPHERALS_SMTP,
                json={"password": "pw", "host": "smtp.example.com"},
                headers=auth_headers(),
            )
            mock_patch_db.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 503, (
        f"SecretResolverUnavailable must map to 503; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_smtp_empty_password_clears_secret(client) -> None:
    """PATCH /admin/peripherals/smtp with password="" calls set_smtp_password("") to clear.

    spec: spec/API.md §Admin (/admin) — an empty-string secret clears it.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_pw = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_smtp_password", mock_set_pw),
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=False),
            patch("src.api.routers.admin.invalidate_smtp_password_cache", MagicMock()),
        ):
            resp = await client.patch(
                _PERIPHERALS_SMTP,
                json={"password": ""},
                headers=auth_headers(),
            )
            mock_set_pw.assert_called_once_with("")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["password"] == ""


@pytest.mark.asyncio
async def test_patch_smtp_omitting_password_does_not_touch_secret(client) -> None:
    """PATCH /admin/peripherals/smtp without password field does not call set_smtp_password.

    Omitting password means "leave unchanged" — the Secret write must be skipped.

    spec: spec/API.md §Admin (/admin) — a secret field omitted from the body leaves
        the Secret unchanged.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_pw = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_smtp_password", mock_set_pw),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_SMTP,
                json={"host": "smtp-new.example.com"},
                headers=auth_headers(),
            )
            mock_set_pw.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_smtp_non_secret_fields_written_to_db(client) -> None:
    """PATCH /admin/peripherals/smtp with non-secret fields calls the DB patch.

    Non-secret fields (host, from_address, port, ...) are written to the DB via
    patch_peripheral_config keyed on "smtp".

    spec: spec/feature/BACKEND.md §SMTP Peripheral — non-secret fields live in the
        peripheral_config DB row under key smtp.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ) as mock_patch_db,
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_SMTP,
                json={"host": "smtp-new.example.com", "from_address": "no-reply@example.com"},
                headers=auth_headers(),
            )
            mock_patch_db.assert_called_once()
            call_args = mock_patch_db.call_args
            assert len(call_args.args) >= 2 and call_args.args[1] == "smtp", (
                f"patch_peripheral_config must be called with 'smtp' as second arg; "
                f"got args={call_args.args!r}, kwargs={call_args.kwargs!r}."
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_internal_patch_smtp_valid_token_returns_200(client) -> None:
    """PATCH /internal/admin/peripherals/smtp with correct X-Internal-Token → 200.

    spec: API.md §Internal Admin (/internal/admin) — valid token grants access;
        `/internal/admin/peripherals/smtp` mirrors the admin PATCH.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_SMTP_DTO),
            ),
            patch("src.api.routers.admin.smtp_password_is_set", return_value=True),
        ):
            resp = await client.patch(
                _INTERNAL_SMTP,
                json={"host": "smtp.example.com"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "smtp.example.com"


@pytest.mark.asyncio
async def test_internal_patch_smtp_without_token_returns_401(client) -> None:
    """PATCH /internal/admin/peripherals/smtp without X-Internal-Token → 401.

    spec: API.md §Internal Admin (/internal/admin) — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(_INTERNAL_SMTP, json={"host": "smtp.example.com"})
    assert resp.status_code == 401
