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
from unittest.mock import AsyncMock, MagicMock, call, patch

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


def _fake_db(dto_for_get=None, updated_at=None, health_row=None) -> tuple:
    """Return (db_mock, override_fn) with scalar_one_or_none returning a mock row.

    The first execute() call satisfies require_authenticated's user lookup
    (returns an Admin User mock).  Subsequent calls return a PeripheralConfig
    row so that service/route logic sees expected data.

    The ``peripheral_health`` SELECT is routed separately and yields ``None`` by
    default: an absent row is the spec's "nothing has reported yet" state, which
    the route must render as ``status: "unknown"``
    (spec/feature/BACKEND_SCHEMA.md §peripheral_health — "Absence of a row and
    ``status='unknown'`` mean the same thing to readers").  Pass ``health_row`` to
    exercise a reported status instead.
    """
    db = AsyncMock()
    row_mock = MagicMock()
    row_mock.updated_at = updated_at or datetime.now(tz=UTC)

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user(role="Admin")

    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = row_mock

    health_result = MagicMock()
    health_result.scalar_one_or_none.return_value = health_row
    health_result.scalar_one.return_value = health_row

    # The auth user-lookup hits the users table; the PeripheralConfig reads are routed
    # by their own table, so an added/reordered config query keeps its correct result.
    route_db_execute(
        db,
        [("users", auth_result), ("peripheral_health", health_result)],
        default=row_result,
    )
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    async def _gen():
        yield db

    return db, _gen


@pytest.fixture(autouse=True)
def _kafka_password_unset():
    """Report the Kafka SASL password as unset unless a test says otherwise.

    ``datahub_kafka_sasl_password_is_set`` reads a Kubernetes Secret, which is
    unreachable out of cluster; every DataHub GET and PATCH consults it (GET to
    mask the field, PATCH to decide whether a stored password must be cleared).
    Defaulting it to ``False`` keeps the unrelated tests in this module cluster-free.
    Tests about the Kafka credential patch it explicitly, which shadows this.

    spec: spec/API.md §DataHub Kafka security — ``kafka_sasl_password`` is
    "Write-only, same ``""`` unset / ``"********"`` set convention as ``token``".
    """
    with patch(
        "src.api.routers.admin.datahub_kafka_sasl_password_is_set", return_value=False
    ) as mock_is_set:
        yield mock_is_set


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
    # Not once-only: the handler also bypasses the cache before reading the stored
    # config to build the effective Kafka tuple it validates the body against
    # (spec/API.md §DataHub Kafka security — "Every rule below is evaluated against
    # the effective tuple"). What matters is that no cached read survives the PATCH.
    assert mock_invalidate.call_args_list, "the datahub config cache must be invalidated"
    assert all(c == call("datahub") for c in mock_invalidate.call_args_list), (
        f"only the datahub peripheral may be invalidated; got {mock_invalidate.call_args_list!r}"
    )

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


# ── Kafka security tuple on GET/PATCH /admin/peripherals/datahub ─────────────
#
# spec: spec/API.md §DataHub Kafka security — the field table, the seven validation
# rules evaluated against the effective tuple, the masking convention, and the
# stored-password clearing on a switch to AWS_MSK_IAM.

_FAKE_DH_DTO_SCRAM = DatahubConfigDTO(
    gms_url="http://gms:8080",
    kafka_brokers="kafka:9093",
    kafka_security_protocol="SASL_SSL",
    kafka_sasl_mechanism="SCRAM-SHA-512",
    kafka_sasl_username="dataspoke",
    kafka_aws_region="",
    kafka_sasl_password_version=2,
)

_MSK_BROKERS = "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098"


@pytest.mark.asyncio
async def test_get_datahub_returns_the_kafka_tuple_with_a_masked_password(client) -> None:
    """GET reports every stored Kafka field and masks the password as "********".

    spec: API.md §DataHub Kafka security — the field table; ``kafka_sasl_password`` is
    "Write-only, same ``""`` unset / ``"********"`` set convention as ``token``".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set",
                return_value=True,
            ),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["kafka_security_protocol"] == "SASL_SSL"
    assert body["kafka_sasl_mechanism"] == "SCRAM-SHA-512"
    assert body["kafka_sasl_username"] == "dataspoke"
    assert body["kafka_aws_region"] == ""
    assert body["kafka_sasl_password_version"] == 2
    assert body["kafka_sasl_password"] == "********"


@pytest.mark.asyncio
async def test_get_datahub_reports_an_unset_kafka_password_as_empty(client) -> None:
    """An unset Kafka credential reads back as ``""``.

    spec: API.md §DataHub Kafka security — the ``""`` unset / ``"********"`` set
    convention.
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set",
                return_value=False,
            ),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.json()["kafka_sasl_password"] == ""


@pytest.mark.asyncio
async def test_is_configured_ignores_the_kafka_password(client) -> None:
    """A DataHub peripheral is configured on the GMS token alone.

    The Kafka credential is optional; a REST-only deployment must not be reported as
    unconfigured for lacking one, and setting one must not make an unwired peripheral
    look configured.

    spec: API.md §DataHub Kafka security — the Kafka fields "do not participate in
    ``is_configured``, and a DataHub peripheral without them is fully configured for
    every REST-based flow"; §Admin — "For DataHub the participating secret is ``token``
    alone — ``kafka_sasl_password`` is optional and never affects the flag."
    """
    for token_set, kafka_set, expected in [
        (True, False, True),
        (True, True, True),
        (False, True, False),
        (False, False, False),
    ]:
        _, db_gen = _fake_db()
        app.dependency_overrides[get_db] = db_gen
        try:
            with (
                patch(
                    "src.api.routers.admin.get_peripheral_config",
                    AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
                ),
                patch("src.api.routers.admin.datahub_token_is_set", return_value=token_set),
                patch(
                    "src.api.routers.admin.datahub_kafka_sasl_password_is_set",
                    return_value=kafka_set,
                ),
            ):
                resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert resp.json()["is_configured"] is expected, (
            f"token_set={token_set}, kafka_password_set={kafka_set} "
            f"must yield is_configured={expected}"
        )


@pytest.mark.asyncio
async def test_get_datahub_reports_unknown_health_when_nothing_has_reported(client) -> None:
    """With no ``peripheral_health`` row the response carries ``status: "unknown"``.

    spec: API.md §DataHub Kafka security — "``status`` is ``unknown`` when the consumer
    has never reported — including every deployment that runs no consumer at all."
    """
    _, db_gen = _fake_db(health_row=None)
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    health = resp.json()["health"]
    assert health["status"] == "unknown"
    assert health["last_error"] is None
    assert health["last_ok_at"] is None


@pytest.mark.asyncio
async def test_get_datahub_surfaces_a_reported_connection_failure(client) -> None:
    """A consumer-reported failure reaches the admin response as ``error`` + message.

    ``is_configured`` alone cannot distinguish a working setup from a wrong mechanism or
    an unauthorized IAM role; this is the field that can.

    spec: API.md §DataHub Kafka security — "The ``health`` object on ``GET`` reports
    whether that configuration actually works"; feature/BACKEND.md §Health reporting.
    """
    reported_at = datetime.now(tz=UTC)
    health_row = MagicMock()
    health_row.status = "error"
    health_row.last_error = "SASL authentication error: Authentication failed"
    health_row.last_ok_at = None
    health_row.updated_at = reported_at

    _, db_gen = _fake_db(health_row=health_row)
    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    body = resp.json()
    assert body["health"]["status"] == "error"
    assert body["health"]["last_error"] == "SASL authentication error: Authentication failed"
    # is_configured is unaffected: the values are present, they just do not work.
    assert body["is_configured"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_datahub_response_reads_health_and_api_health_from_two_distinct_rows(
    client, method: str
) -> None:
    """``health`` renders the ``datahub`` row and ``api_health`` the ``datahub-api`` row.

    The two rows are seeded with opposite verdicts and routed by the row name the
    query carries, so a route that read one row for both fields — the conflation the
    two-row design exists to prevent — cannot pass: it would report the same status
    twice.

    Both verbs are driven because ``DatahubPeripheralResponse`` is the PATCH response
    model too, and the router reads the ``datahub-api`` row at **two** independent call
    sites (``get_datahub_peripheral`` and ``_apply_datahub_patch_and_respond``). Covering
    only GET leaves the second one free to read the wrong row name.

    spec: feature/BACKEND.md §Health reporting — "``GET /admin/peripherals/datahub``
    returns the first as ``health`` and the second as ``api_health``"; the table binds
    ``datahub`` to the event stream (event consumer) and ``datahub-api`` to the
    metadata API (sync sweep).
    spec: feature/BACKEND.md §Health reporting — "**Two rows, not one.** … A single
    shared row would let the consumer and the sweep overwrite each other's verdict".
    spec: API.md §Admin (/admin) — PATCH returns the same peripheral representation as
    GET.
    """
    kafka_row = MagicMock()
    kafka_row.status = "error"
    kafka_row.last_error = "KafkaError{code=_ALL_BROKERS_DOWN}"
    kafka_row.last_ok_at = None
    kafka_row.updated_at = datetime.now(tz=UTC)

    api_row = MagicMock()
    api_row.status = "ok"
    api_row.last_error = None
    api_row.last_ok_at = datetime.now(tz=UTC)
    api_row.updated_at = datetime.now(tz=UTC)

    def _health_result(row):
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        result.scalar_one.return_value = row
        return result

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user(role="Admin")
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = MagicMock(updated_at=datetime.now(tz=UTC))

    db = AsyncMock()
    # 'datahub-api' is routed first: the substring "'datahub'" does not appear in the
    # hyphenated literal, but routing the broader matcher first would shadow it.
    route_db_execute(
        db,
        [
            ("users", auth_result),
            ("'datahub-api'", _health_result(api_row)),
            ("peripheral_health", _health_result(kafka_row)),
        ],
        default=config_result,
    )

    async def _gen():
        yield db

    app.dependency_overrides[get_db] = _gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            if method == "GET":
                resp = await client.get(_PERIPHERALS_DH, headers=auth_headers())
            else:
                resp = await client.patch(
                    _PERIPHERALS_DH,
                    json={"gms_url": "http://gms:8080"},
                    headers=auth_headers(),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, f"{method}: {resp.text}"
    body = resp.json()
    assert body["health"]["status"] == "error", (
        f"{method}: 'health' must render the 'datahub' row (the event stream). "
        "spec: feature/BACKEND.md §Health reporting."
    )
    assert body["health"]["last_error"] == "KafkaError{code=_ALL_BROKERS_DOWN}"
    assert body["api_health"]["status"] == "ok", (
        f"{method}: 'api_health' must render the 'datahub-api' row (the GMS metadata "
        "API), which here reports the opposite verdict. "
        "spec: feature/BACKEND.md §Health reporting."
    )
    assert body["api_health"]["last_error"] is None
    assert body["api_health"]["last_ok_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "body", "field", "rule"),
    [
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9093"),
            {"kafka_security_protocol": "SASL_SSL"},
            "kafka_sasl_mechanism",
            "rule 1 — a mechanism is required with a SASL protocol",
            id="rule1-sasl-protocol-without-mechanism",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9093"),
            {"kafka_sasl_mechanism": "PLAIN"},
            "kafka_sasl_mechanism",
            "rule 1 — a mechanism is rejected with PLAINTEXT",
            id="rule1-mechanism-without-sasl-protocol",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9093"),
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": "SCRAM-SHA-512"},
            "kafka_sasl_username",
            "rule 2 — a credential mechanism needs a username",
            id="rule2-scram-without-username",
        ),
        pytest.param(
            _FAKE_DH_DTO_SCRAM,
            {"kafka_sasl_mechanism": "AWS_MSK_IAM", "kafka_brokers": _MSK_BROKERS},
            "kafka_sasl_username",
            "rule 3 — a STORED username blocks the switch to AWS_MSK_IAM",
            id="rule3-effective-tuple-carries-the-stored-username",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS),
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_sasl_password": "typed-credential",
            },
            "kafka_sasl_password",
            "rule 3 — a submitted password is rejected under AWS_MSK_IAM",
            id="rule3-submitted-password-under-msk-iam",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS),
            {"kafka_security_protocol": "SASL_PLAINTEXT", "kafka_sasl_mechanism": "AWS_MSK_IAM"},
            "kafka_security_protocol",
            "rule 4 — AWS_MSK_IAM requires SASL_SSL, never a silent upgrade",
            id="rule4-msk-iam-on-an-unencrypted-wire",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9093"),
            {"kafka_aws_region": "us-east-1"},
            "kafka_aws_region",
            "rule 5 — a region is accepted only with AWS_MSK_IAM",
            id="rule5-region-without-msk-iam",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka.evil.tld:9098"),
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": "AWS_MSK_IAM"},
            "kafka_brokers",
            "rule 6 — every broker host must have the MSK broker shape under AWS_MSK_IAM",
            id="rule6-msk-iam-pointed-at-a-foreign-host",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS),
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_brokers": "ec2-203-0-113-25.compute-1.amazonaws.com:9098",
            },
            "kafka_brokers",
            "rule 6 — an AWS host that is not an MSK broker is still refused",
            id="SECURITY-rule6-aws-host-that-is-not-an-msk-broker",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS),
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_aws_region": "eu-west-1",
            },
            "kafka_aws_region",
            "rule 7 — an explicit region contradicting the stored broker hosts",
            id="SECURITY-rule7-region-contradicting-the-hosts",
        ),
        pytest.param(
            DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS),
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "AWS_MSK_IAM",
                "kafka_brokers": (
                    "b-1.imazon.abc.c2.kafka.us-east-1.amazonaws.com:9098,"
                    "b-2.imazon.abc.c2.kafka.eu-west-1.amazonaws.com:9098"
                ),
            },
            "kafka_brokers",
            "rule 7 — a mixed-region broker list",
            id="SECURITY-rule7-mixed-region-broker-list",
        ),
    ],
)
async def test_patch_datahub_rejects_a_rule_violation_with_422(
    client, stored, body, field, rule
) -> None:
    """Each of the seven rules rejects with ``422 INVALID_PARAMETER`` naming the field.

    Every case is judged against the **effective** tuple — the stored settings with the
    body merged over them — which is why the rule-3 case supplies only a mechanism and is
    still rejected for the username already in storage.

    spec: API.md §DataHub Kafka security — the seven-rule table; "**Validation is
    normative and every violation is ``422 INVALID_PARAMETER``** — the existing generic
    code, with the offending field named in ``detail``"; "Every rule below is evaluated
    against the **effective tuple** — the stored settings with the ``PATCH`` body merged
    over them".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_patch_db = AsyncMock(return_value=stored)
    mock_set_password = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.get_peripheral_config", AsyncMock(return_value=stored)),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.set_datahub_kafka_sasl_password", mock_set_password),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(_PERIPHERALS_DH, json=body, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422, f"{rule}: expected 422, got {resp.status_code} {resp.text}"
    payload = resp.json()
    assert payload["error_code"] == "INVALID_PARAMETER", payload
    assert payload["detail"]["field"] == field, (
        f"{rule}: the offending field must be named in detail; got {payload.get('detail')}"
    )
    # Nothing may be written when the request is refused.
    mock_patch_db.assert_not_called()
    mock_set_password.assert_not_called()


@pytest.mark.asyncio
async def test_patch_datahub_accepts_a_valid_msk_iam_switch(client) -> None:
    """Clearing the stored username in the same PATCH makes the AWS_MSK_IAM switch valid.

    The backstop for the rule-3 rejection above: the request is refused for the leftover
    credential, not for selecting IAM.

    spec: API.md §DataHub Kafka security — "the operator clears it in the same ``PATCH``
    with an explicit ``""``".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO_SCRAM)
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.set_datahub_kafka_sasl_password", MagicMock()),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set", return_value=False
            ),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={
                    "kafka_brokers": _MSK_BROKERS,
                    "kafka_security_protocol": "SASL_SSL",
                    "kafka_sasl_mechanism": "AWS_MSK_IAM",
                    "kafka_sasl_username": "",
                },
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    mock_patch_db.assert_called_once()


@pytest.mark.asyncio
async def test_patch_datahub_kafka_password_routes_to_secret_and_bumps_the_version(
    client,
) -> None:
    """The Kafka password goes to the K8s Secret, never the DB, and moves the counter.

    The counter is what turns a Secret-only rotation into a DB-plane change the running
    consumer detects.

    spec: API.md §DataHub Kafka security — "Routed to ``dataspoke-datahub-secret`` key
    ``kafka_sasl_password``, never the DB"; ``kafka_sasl_password_version`` "Incremented
    by ``PATCH`` whenever the password Secret is written".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_password = MagicMock()
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO_SCRAM)
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.set_datahub_kafka_sasl_password", mock_set_password),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set", return_value=True
            ),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"kafka_sasl_password": "rotated-secret"},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    mock_set_password.assert_called_once_with("rotated-secret")

    _, kwargs = mock_patch_db.call_args
    assert kwargs.get("bump_kafka_sasl_password_version") is True, (
        f"the password write must bump the version counter; call was {mock_patch_db.call_args!r}"
    )
    assert "kafka_sasl_password" not in kwargs, (
        "the plaintext password must never be handed to the DB writer"
    )
    assert "rotated-secret" not in resp.text, "the plaintext must never appear in the response"


@pytest.mark.asyncio
async def test_patch_datahub_clears_a_stored_password_when_switching_to_msk_iam(
    client,
) -> None:
    """A stored password is cleared once the effective mechanism becomes ``AWS_MSK_IAM``.

    Leaving it would keep a live credential in the Secret that nothing reads and that GET
    would keep reporting as ``"********"``.

    spec: API.md §DataHub Kafka security — "The **stored password is handled differently:
    it is cleared** whenever the effective mechanism becomes ``AWS_MSK_IAM``, and ``GET``
    reports ``kafka_sasl_password: ""`` from then on."
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_password = MagicMock()
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO_SCRAM)
    stored = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers=_MSK_BROKERS)
    try:
        with (
            patch("src.api.routers.admin.get_peripheral_config", AsyncMock(return_value=stored)),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.set_datahub_kafka_sasl_password", mock_set_password),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set", return_value=True
            ),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={
                    "kafka_security_protocol": "SASL_SSL",
                    "kafka_sasl_mechanism": "AWS_MSK_IAM",
                },
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    # A plain assert, not `assert_called_once_with(...), ("msg")`: the mock assert family
    # takes no message argument, so a trailing tuple silences nothing and hides that the
    # check ran at all (spec/TESTING.md §Assertion Discipline — "No dead assertion-message
    # tuples").
    assert mock_set_password.call_args_list == [call("")], (
        "the stored credential must be cleared exactly once, not left in the Secret; got "
        f"{mock_set_password.call_args_list}"
    )
    _, kwargs = mock_patch_db.call_args
    assert kwargs.get("bump_kafka_sasl_password_version") is True, (
        "clearing the password is a Secret write, so the counter must move too"
    )


@pytest.mark.asyncio
async def test_patch_datahub_leaves_the_kafka_password_alone_when_not_supplied(client) -> None:
    """Omitting the field leaves the Secret untouched — the backstop for the clearing test.

    Without this, the clearing assertion above could pass for a handler that rewrites the
    Secret on every PATCH.

    spec: API.md §Admin — "A secret field omitted from the body leaves the Secret
    unchanged".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_set_password = MagicMock()
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch(
                "src.api.routers.admin.patch_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.set_datahub_kafka_sasl_password", mock_set_password),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
            patch(
                "src.api.routers.admin.datahub_kafka_sasl_password_is_set", return_value=True
            ),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"kafka_brokers": "kafka-new:9093"},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    mock_set_password.assert_not_called()


@pytest.mark.asyncio
async def test_patch_datahub_rejects_a_client_supplied_password_version(client) -> None:
    """``kafka_sasl_password_version`` is API-owned and not accepted on PATCH.

    An operator who could set the counter could make a rotation invisible to the consumer.

    spec: API.md §DataHub Kafka security — the counter is "Incremented by ``PATCH``
    whenever the password Secret is written"; src/api/schemas/admin.py
    DatahubPeripheralPatchRequest — "``kafka_sasl_password_version`` is not accepted —
    the API owns the counter".
    """
    _, db_gen = _fake_db()
    app.dependency_overrides[get_db] = db_gen
    mock_patch_db = AsyncMock(return_value=_FAKE_DH_DTO_SCRAM)
    try:
        with (
            patch(
                "src.api.routers.admin.get_peripheral_config",
                AsyncMock(return_value=_FAKE_DH_DTO_SCRAM),
            ),
            patch("src.api.routers.admin.patch_peripheral_config", mock_patch_db),
            patch("src.api.routers.admin.datahub_token_is_set", return_value=True),
        ):
            resp = await client.patch(
                _PERIPHERALS_DH,
                json={"kafka_brokers": "kafka:9093", "kafka_sasl_password_version": 99},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    _, kwargs = mock_patch_db.call_args
    assert "kafka_sasl_password_version" not in kwargs, (
        "a client-supplied counter must never reach the DB writer; "
        f"call was {mock_patch_db.call_args!r}"
    )
