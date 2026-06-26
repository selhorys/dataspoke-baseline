"""Unit tests for admin conf routes (GET /admin/conf, PATCH /admin/conf,
PATCH /internal/admin/conf).

Routes under test:
  GET  /api/v1/admin/conf            — requires Admin role
  PATCH /api/v1/admin/conf           — requires Admin role
  PATCH /internal/admin/conf         — requires X-Internal-Token header

Concerns covered:

1. Auth gates:
   - GET  /admin/conf without JWT → 401
   - GET  /admin/conf with non-Admin role → 403
   - PATCH /admin/conf without JWT → 401
   - PATCH /admin/conf with non-Admin role → 403
   - PATCH /internal/admin/conf without X-Internal-Token → 401
   - PATCH /internal/admin/conf with wrong token → 401

2. GET /admin/conf with Admin role:
   - Returns 200.
   - Response contains all 23 expected fields (21 config fields + updated_at + resp_time).
   - Response contains resp_time (SingleResponse envelope).
   - Response contains updated_at.
   - llm_api_key is masked: "********" when set, "" when unset — never plaintext.

3. PATCH /admin/conf with valid partial body:
   - Returns 200.
   - Response reflects updated fields.
   - PATCH with llm_api_key calls set_llm_api_key(), NOT patch_runtime_config with it.
   - PATCH with llm_api_key="" calls set_llm_api_key("") (clear).

4. PATCH /admin/conf with out-of-bounds value → 422 (schema rejects before service).

5. PATCH /admin/conf with SecretResolverUnavailable (out-of-cluster) → 503.

6. PATCH /internal/admin/conf with correct token → 200.

Spec traceability:
- spec/feature/BACKEND_LLM.md §LLM API key — masked GET, write to Secret on PATCH,
  SecretResolverUnavailable → 503.
- spec/API.md §Access Control — Admin role required for /admin/*
- spec/API.md §Internal routes — X-Internal-Token required for /internal/…
- src/api/schemas/admin.py RuntimeConfResponse — resp_time, updated_at, masked llm_api_key.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.dependencies import get_db
from src.api.main import app
from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO
from src.shared.db.models import RuntimeConfig
from src.shared.secrets import SecretResolverUnavailable
from tests.unit.api.conftest import _make_mock_user, auth_headers

_ADMIN_CONF = "/api/v1/admin/conf"
_INTERNAL_CONF = "/internal/admin/conf"
_INTERNAL_TOKEN = "test-internal-secret"

# The full set of keys expected in every RuntimeConfResponse.
_EXPECTED_RESPONSE_KEYS = {
    "llm_provider",
    "llm_model",
    "llm_api_key",
    "ontogen_llm_max_iterations",
    "ontogen_debate_max_turns",
    "ontogen_debate_rag_k",
    "ontogen_debate_reviewer_model",
    "metagen_llm_max_iterations",
    "metagen_debate_max_turns",
    "metagen_debate_rag_k",
    "metagen_debate_reviewer_model",
    "metagen_confidence_threshold",
    "metagen_ontology_rag_node_k",
    "metagen_ontology_rag_edge_k",
    "metagen_ontology_rag_triple_k",
    "validation_score_n_intervals",
    "stub_redis_client",
    "stub_llm_client",
    "stub_pgvector_manager",
    "stub_notification_service",
    "auth_datahub_corp_group",
    "updated_at",
    "resp_time",
}


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_dto(**overrides) -> RuntimeConfigDTO:
    """Build a RuntimeConfigDTO from RUNTIME_CONFIG_DEFAULTS + overrides."""
    return RuntimeConfigDTO(**{**RUNTIME_CONFIG_DEFAULTS, **overrides})


def _make_row_mock(**overrides) -> MagicMock:
    """Build a mock RuntimeConfig ORM row."""
    row = MagicMock(spec=RuntimeConfig)
    for field, value in {**RUNTIME_CONFIG_DEFAULTS, **overrides}.items():
        setattr(row, field, value)
    row.id = 1
    row.updated_at = datetime.now(tz=UTC)
    return row


def _fake_db_with_row(row) -> tuple:
    """Return (db_mock, override_fn) for dependency injection.

    The first execute() call satisfies require_authenticated's user lookup
    (returns an Admin User mock).  Subsequent calls return the provided row
    so that service/route logic sees the expected RuntimeConfig data.
    """
    db = AsyncMock()

    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user(role="Admin")

    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = row

    db.execute = AsyncMock(side_effect=[auth_result, row_result, row_result, row_result])
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()

    async def _gen():
        yield db

    return db, _gen


# ── 1a. Auth: GET /admin/conf ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /admin/conf without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.get(_ADMIN_CONF)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_conf_non_admin_role_returns_403(client) -> None:
    """GET /admin/conf with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext
    from tests.unit.api.conftest import _make_mock_user

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.get(_ADMIN_CONF, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


@pytest.mark.asyncio
async def test_get_conf_editor_role_returns_403(client) -> None:
    """GET /admin/conf with Editor role returns 403.

    spec: API.md §Admin routes — Admin role required exclusively.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext
    from tests.unit.api.conftest import _make_mock_user

    editor_ctx = AuthContext(user=_make_mock_user(role="Editor"), effective_role="Editor")
    app.dependency_overrides[require_authenticated] = lambda: editor_ctx
    try:
        resp = await client.get(_ADMIN_CONF, headers=auth_headers())
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


# ── 1b. Auth: PATCH /admin/conf ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_without_token_returns_401(client) -> None:
    """PATCH /admin/conf without JWT returns 401.

    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await client.patch(_ADMIN_CONF, json={"llm_model": "gpt-4o-mini"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_conf_non_admin_role_returns_403(client) -> None:
    """PATCH /admin/conf with non-Admin role returns 403.

    spec: API.md §Admin routes — Admin role required exclusively.
    """
    from src.api.auth.dependencies import require_authenticated
    from src.backend.auth.privilege import AuthContext
    from tests.unit.api.conftest import _make_mock_user

    reader_ctx = AuthContext(user=_make_mock_user(role="Reader"), effective_role="Reader")
    app.dependency_overrides[require_authenticated] = lambda: reader_ctx
    try:
        resp = await client.patch(
            _ADMIN_CONF,
            json={"llm_model": "gpt-4o-mini"},
            headers=auth_headers(),
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


# ── 1c. Auth: PATCH /internal/admin/conf ─────────────────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_conf_unset_token_returns_503(client) -> None:
    """PATCH /internal/admin/conf when DATASPOKE_INTERNAL_TOKEN is blank returns 503.

    spec: src/api/auth/internal.py require_internal_token — 503 with
    error_code 'INTERNAL_AUTH_NOT_CONFIGURED' when settings.internal_token is falsy.

    This pins the unset-token branch at the unit layer, independent of api-wired tests.
    """
    with patch("src.shared.settings.settings.internal_token", ""):
        resp = await client.patch(
            _INTERNAL_CONF,
            json={"llm_model": "gpt-4o-mini"},
            headers={"X-Internal-Token": "any-value"},
        )
    assert resp.status_code == 503, (
        f"Blank internal_token must yield 503 SERVICE_UNAVAILABLE; got {resp.status_code}"
    )
    body = resp.json()
    assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED", (
        f"error_code must be 'INTERNAL_AUTH_NOT_CONFIGURED'; got: {body}"
    )


@pytest.mark.asyncio
async def test_internal_patch_conf_without_token_returns_401(client) -> None:
    """PATCH /internal/admin/conf without X-Internal-Token returns 401.

    spec: API.md §Internal routes — X-Internal-Token required.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(_INTERNAL_CONF, json={"llm_model": "gpt-4o-mini"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_patch_conf_wrong_token_returns_401(client) -> None:
    """PATCH /internal/admin/conf with wrong token returns 401.

    spec: API.md §Internal routes — constant-time compare; mismatch → 401.
    """
    with patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN):
        resp = await client.patch(
            _INTERNAL_CONF,
            json={"llm_model": "gpt-4o-mini"},
            headers={"X-Internal-Token": "wrong-token"},
        )
    assert resp.status_code == 401


# ── 2. GET /admin/conf — response shape ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_returns_200_with_all_22_fields(client) -> None:
    """GET /admin/conf with Admin role returns 200 with EXACTLY the 23 expected fields.

    The 23 fields are: 21 config fields (15 DB tunables + 4 stub booleans +
    llm_api_key masked indicator + auth_datahub_corp_group) + updated_at + resp_time.
    Any extra or missing key fails the assertion.

    spec: BACKEND_LLM.md §LLM API key — GET returns llm_api_key as masked indicator.
    spec: plan §stub toggles — four stub_* fields added to RuntimeConfResponse.
    """
    row = _make_row_mock()
    db, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=_make_dto()),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.get(_ADMIN_CONF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()

    actual_keys = set(body.keys())
    missing = _EXPECTED_RESPONSE_KEYS - actual_keys
    extra = actual_keys - _EXPECTED_RESPONSE_KEYS
    assert not missing, f"Response missing expected fields: {missing}"
    assert not extra, (
        f"Response contains unexpected extra fields: {extra}. "
        "An accidental secret or extra field must not leak into the response."
    )


@pytest.mark.asyncio
async def test_get_conf_includes_resp_time(client) -> None:
    """GET /admin/conf response includes resp_time (SingleResponse envelope).

    spec: API.md §SingleResponse — resp_time included on every response.
    """
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=_make_dto()),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.get(_ADMIN_CONF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "resp_time" in body, (
        "resp_time must be present in every RuntimeConfResponse "
        "(spec: API.md §SingleResponse envelope)"
    )


@pytest.mark.asyncio
async def test_get_conf_includes_updated_at(client) -> None:
    """GET /admin/conf response includes updated_at from the DB row."""
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=_make_dto()),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.get(_ADMIN_CONF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "updated_at" in body


# ── 2b. GET /admin/conf — llm_api_key masking ────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_llm_api_key_masked_when_set(client) -> None:
    """GET /admin/conf returns llm_api_key="********" (not plaintext) when the key is set.

    spec: BACKEND_LLM.md §LLM API key — masked GET; plaintext never returned.
    """
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=_make_dto()),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=True),
        ):
            resp = await client.get(_ADMIN_CONF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_api_key"] == "********", (
        f"llm_api_key must be masked '********' when set; got {body['llm_api_key']!r}. "
        "Plaintext must never appear in the response."
    )


@pytest.mark.asyncio
async def test_get_conf_llm_api_key_empty_when_unset(client) -> None:
    """GET /admin/conf returns llm_api_key="" when no key has been stored.

    spec: BACKEND_LLM.md §LLM API key — masked GET returns "" when unset.
    """
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=_make_dto()),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.get(_ADMIN_CONF, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_api_key"] == "", (
        f"llm_api_key must be '' when unset; got {body['llm_api_key']!r}"
    )


# ── 3. PATCH /admin/conf — happy path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_returns_200_with_updated_fields(client) -> None:
    """PATCH /admin/conf with valid partial body returns 200 reflecting updated values.

    spec: task brief — PATCH with partial body returns 200 with updated values.
    """
    patched_dto = _make_dto(llm_model="gpt-4o-mini", ontogen_debate_max_turns=6)
    row = _make_row_mock(llm_model="gpt-4o-mini", ontogen_debate_max_turns=6)
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch(
                "src.api.routers.admin.patch_runtime_config",
                AsyncMock(return_value=patched_dto),
            ),
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=patched_dto),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.patch(
                _ADMIN_CONF,
                json={"llm_model": "gpt-4o-mini", "ontogen_debate_max_turns": 6},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_model"] == "gpt-4o-mini"
    assert body["ontogen_debate_max_turns"] == 6


@pytest.mark.asyncio
async def test_patch_conf_llm_api_key_routes_to_secret_not_db(client) -> None:
    """PATCH /admin/conf with llm_api_key calls set_llm_api_key() and NOT patch_runtime_config
    with that key — the Secret write is the only persistent action for this field.

    spec: BACKEND_LLM.md §LLM API key — PATCH routes llm_api_key to the Secret.
    """
    patched_dto = _make_dto()
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    mock_set_key = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_llm_api_key", mock_set_key),
            patch(
                "src.api.routers.admin.patch_runtime_config",
                AsyncMock(return_value=patched_dto),
            ) as mock_patch_db,
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=True),
        ):
            resp = await client.patch(
                _ADMIN_CONF,
                json={"llm_api_key": "sk-test-key"},
                headers=auth_headers(),
            )

            # set_llm_api_key must be called with the plaintext value.
            mock_set_key.assert_called_once_with("sk-test-key")

            # patch_runtime_config must NOT receive llm_api_key — it's not a DB column.
            call_kwargs = mock_patch_db.call_args
            if call_kwargs is not None:
                _, kwargs = call_kwargs
                assert "llm_api_key" not in kwargs, (
                    "llm_api_key must never be passed to patch_runtime_config — "
                    "it is stored in the Kubernetes Secret, not the DB."
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    # Response masks the key — plaintext "sk-test-key" must not appear.
    assert body["llm_api_key"] == "********"
    assert "sk-test-key" not in str(body), (
        "Plaintext key must never appear in the response body."
    )


@pytest.mark.asyncio
async def test_patch_conf_llm_api_key_empty_string_clears_key(client) -> None:
    """PATCH /admin/conf with llm_api_key="" calls set_llm_api_key("") to clear the key.

    An explicit "" is a clear operation, not "leave unchanged".

    spec: BACKEND_LLM.md §LLM API key — explicit "" clears the key.
    """
    patched_dto = _make_dto()
    row = _make_row_mock()
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    mock_set_key = MagicMock()
    try:
        with (
            patch("src.api.routers.admin.set_llm_api_key", mock_set_key),
            patch(
                "src.api.routers.admin.patch_runtime_config",
                AsyncMock(return_value=patched_dto),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.patch(
                _ADMIN_CONF,
                json={"llm_api_key": ""},
                headers=auth_headers(),
            )

            mock_set_key.assert_called_once_with("")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_api_key"] == ""


# ── 4. PATCH /admin/conf — out-of-bounds value → 422 ─────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_out_of_bounds_value_returns_422(client) -> None:
    """PATCH /admin/conf with out-of-bounds value returns 422.

    Pydantic validates the request body before the service layer is reached.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — ontogen_debate_max_turns le=10.
    """
    resp = await client.patch(
        _ADMIN_CONF,
        json={"ontogen_debate_max_turns": 11},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, (
        f"ontogen_debate_max_turns=11 exceeds le=10 bound; "
        f"expected 422 but got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_confidence_threshold_above_1_returns_422(client) -> None:
    """PATCH /admin/conf with metagen_confidence_threshold > 1.0 returns 422.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — le=1.0.
    """
    resp = await client.patch(
        _ADMIN_CONF,
        json={"metagen_confidence_threshold": 1.5},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, (
        f"metagen_confidence_threshold=1.5 exceeds le=1.0 bound; "
        f"expected 422 but got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_validation_score_n_intervals_zero_returns_422(client) -> None:
    """PATCH /admin/conf with validation_score_n_intervals=0 returns 422.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — ge=1.
    """
    resp = await client.patch(
        _ADMIN_CONF,
        json={"validation_score_n_intervals": 0},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, (
        f"validation_score_n_intervals=0 violates ge=1 bound; "
        f"expected 422 but got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_llm_api_key_over_8192_chars_returns_422(client) -> None:
    """PATCH /admin/conf with llm_api_key longer than 8192 characters returns 422.

    The schema caps llm_api_key at max_length=8192. Any value exceeding that
    limit must be rejected by Pydantic/FastAPI before the service layer is reached.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — llm_api_key max_length=8192.
    """
    oversized_key = "sk-" + "x" * 8193  # 3 + 8193 = 8196 chars, well above the 8192 cap

    resp = await client.patch(
        _ADMIN_CONF,
        json={"llm_api_key": oversized_key},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, (
        f"llm_api_key longer than 8192 chars must return 422 (schema bound); "
        f"got {resp.status_code}: {resp.text}"
    )


# ── 5. PATCH /admin/conf — SecretResolverUnavailable → 503 ───────────────────


@pytest.mark.asyncio
async def test_patch_conf_llm_api_key_out_of_cluster_returns_503(client) -> None:
    """PATCH /admin/conf with llm_api_key when out-of-cluster returns 503.

    set_llm_api_key raises SecretResolverUnavailable when the Kubernetes API
    is not available (k8s client init failure).  The router maps this to
    StorageUnavailableError → HTTP 503.

    spec: BACKEND_LLM.md §LLM API key — SecretResolverUnavailable → 503.
    """
    _, db_gen = _fake_db_with_row(_make_row_mock())
    app.dependency_overrides[get_db] = db_gen
    try:
        with patch(
            "src.api.routers.admin.set_llm_api_key",
            side_effect=SecretResolverUnavailable("out of cluster"),
        ):
            resp = await client.patch(
                _ADMIN_CONF,
                json={"llm_api_key": "sk-test-key"},
                headers=auth_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 503, (
        f"SecretResolverUnavailable must map to 503; got {resp.status_code}: {resp.text}"
    )


# ── 6. PATCH /internal/admin/conf — valid token → 200 ────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_conf_valid_token_returns_200(client) -> None:
    """PATCH /internal/admin/conf with correct X-Internal-Token returns 200.

    spec: API.md §Internal routes — valid token grants access.
    """
    patched_dto = _make_dto(llm_model="gpt-4o-mini")
    row = _make_row_mock(llm_model="gpt-4o-mini")
    _, db_gen = _fake_db_with_row(row)

    app.dependency_overrides[get_db] = db_gen
    try:
        with (
            patch("src.shared.settings.settings.internal_token", _INTERNAL_TOKEN),
            patch(
                "src.api.routers.admin.patch_runtime_config",
                AsyncMock(return_value=patched_dto),
            ),
            patch(
                "src.api.routers.admin.get_runtime_config",
                AsyncMock(return_value=patched_dto),
            ),
            patch("src.api.routers.admin.llm_api_key_is_set", return_value=False),
        ):
            resp = await client.patch(
                _INTERNAL_CONF,
                json={"llm_model": "gpt-4o-mini"},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_model"] == "gpt-4o-mini"
