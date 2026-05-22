"""Api-wired integration tests for the DB-backed runtime configuration feature.

Routes under test:
  GET  /api/v1/admin/conf            — requires 'admin' group JWT
  PATCH /api/v1/admin/conf           — requires 'admin' group JWT
  PATCH /internal/admin/conf         — requires X-Internal-Token

Concerns covered:

1. GET /admin/conf on a fresh (or reset) DB returns all 15 fields at factory
   defaults, includes resp_time and updated_at, and contains NO secret field.

2. PATCH /admin/conf with a partial body returns 200 with updated values;
   a subsequent GET reflects them.

3. PATCH /admin/conf with out-of-bounds value → 422.

4. Auth: GET/PATCH without JWT → 401; with non-admin JWT → 403.

5. PATCH /internal/admin/conf with valid X-Internal-Token → 200, reflected by
   GET; without / with wrong token → 401 (or 503 if token unset).

IMPORTANT CACHE NOTE: get_runtime_config has a ~30s process cache.  All state
mutations in this file go through the PATCH endpoint, which calls
invalidate_runtime_config_cache() internally before returning.  Tests are
therefore ordered so that each subsequent GET sees the value written by the
preceding PATCH without needing an out-of-band invalidation.

After each mutating test, the test restores the factory defaults via another
PATCH so later tests start from a known baseline.  The cache is automatically
warmed with the restored values by that cleanup PATCH.

Spec traceability:
- task brief §api-wired — all concerns listed above.
- task brief §What's under test — factory defaults, 15 fields, auth rules,
  no-secret-in-response, updated_at, resp_time invariants.
- spec/API.md §Admin routes — admin group required for /admin/…
- spec/API.md §Internal routes — X-Internal-Token for /internal/…
- src/api/schemas/admin.py RuntimeConfResponse, RuntimeConfPatchRequest bounds.
- src/backend/admin/config_service.py RUNTIME_CONFIG_DEFAULTS.
"""

import os

import httpx
import pytest

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS

# Module-level constants — no DataHub/Postgres/Kafka data needed; the
# runtime_config table is managed by alembic and always present in the
# dev-env DB (seeded with factory defaults by the first GET).
# TESTING.md §Per-Module Dummy-Data Reset — omitting constants = no-op.

_ADMIN_CONF = "/api/v1/admin/conf"
_INTERNAL_CONF = "/internal/admin/conf"


# ── Factory default values used in assertions ─────────────────────────────────
# These are the spec-documented values (task brief §What's under test).
# We do NOT derive them from current impl output — they come from the spec.

_EXPECTED_DEFAULTS: dict[str, object] = {
    "llm_provider": "gemini",
    "llm_model": "gemini-3.5-flash",
    "ontogen_llm_max_iterations": 3,
    "ontogen_debate_max_turns": 4,
    "ontogen_debate_rag_k": 5,
    "ontogen_debate_reviewer_model": None,
    "metagen_llm_max_iterations": 3,
    "metagen_debate_max_turns": 4,
    "metagen_debate_rag_k": 5,
    "metagen_debate_reviewer_model": None,
    "metagen_confidence_threshold": 0.7,
    "metagen_ontology_rag_node_k": 5,
    "metagen_ontology_rag_edge_k": 5,
    "metagen_ontology_rag_triple_k": 5,
    "validation_score_n_intervals": 3,
}


async def _reset_to_defaults(api_client: httpx.AsyncClient, admin_headers: dict) -> None:
    """Restore the singleton conf to factory defaults via PATCH.

    Driving state through the endpoint guarantees the process cache is
    invalidated (patch_runtime_config calls invalidate_runtime_config_cache()).
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={
            "llm_provider": "gemini",
            "llm_model": "gemini-3.5-flash",
            "ontogen_llm_max_iterations": 3,
            "ontogen_debate_max_turns": 4,
            "ontogen_debate_rag_k": 5,
            "ontogen_debate_reviewer_model": None,
            "metagen_llm_max_iterations": 3,
            "metagen_debate_max_turns": 4,
            "metagen_debate_rag_k": 5,
            "metagen_debate_reviewer_model": None,
            "metagen_confidence_threshold": 0.7,
            "metagen_ontology_rag_node_k": 5,
            "metagen_ontology_rag_edge_k": 5,
            "metagen_ontology_rag_triple_k": 5,
            "validation_score_n_intervals": 3,
        },
    )
    assert resp.status_code == 200, f"Reset-to-defaults PATCH failed: {resp.text}"


# ── 1. GET /admin/conf — factory defaults, envelope, no-secret ───────────────


@pytest.mark.asyncio
async def test_get_conf_returns_factory_defaults(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf returns all 15 fields at factory defaults.

    After reset to factory defaults the response values MUST match the
    documented factory defaults exactly.

    spec: task brief §api-wired — 'GET on a fresh DB returns all 15 fields at
    factory defaults, includes resp_time, contains NO llm_api_key / no secret key.'
    spec: task brief §What's under test — factory defaults section.
    """
    # Drive to known state via PATCH (cache-safe).
    await _reset_to_defaults(api_client, admin_headers)

    resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)

    assert resp.status_code == 200, f"GET /admin/conf returned {resp.status_code}: {resp.text}"
    body = resp.json()

    for field, expected in _EXPECTED_DEFAULTS.items():
        assert field in body, f"Field '{field}' missing from response"
        assert body[field] == expected, (
            f"Field '{field}': expected {expected!r}, got {body[field]!r}. "
            f"spec: task brief §What's under test — factory default for {field}."
        )


@pytest.mark.asyncio
async def test_get_conf_includes_resp_time_and_updated_at(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf includes resp_time and updated_at in the response.

    spec: task brief §api-wired — 'includes resp_time'.
    spec: task brief §What's under test — 'RuntimeConfResponse: 15 fields +
    updated_at + resp_time'.
    spec: API.md §SingleResponse — resp_time on every response.
    """
    resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "resp_time" in body, "resp_time must be present (spec: API.md §SingleResponse)"
    assert "updated_at" in body, "updated_at must be present (spec: task brief §What's under test)"


@pytest.mark.asyncio
async def test_get_conf_contains_no_secret_fields(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf response contains NO secret keys.

    spec: task brief §api-wired — 'contains NO llm_api_key / no secret key.'
    spec: task brief §What's under test — 'NO secret' in the response schema.
    """
    resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()

    # Any key with 'api_key', 'secret', or 'password' substring indicates a leak.
    secret_like = [k for k in body if any(kw in k for kw in ("api_key", "secret", "password"))]
    assert not secret_like, (
        f"Response must not expose secret fields; found: {secret_like}. "
        "spec: task brief §api-wired — no secret in RuntimeConfResponse."
    )


# ── 2. Auth: GET/PATCH require admin group ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_auth_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /admin/conf without Authorization header returns 401.

    spec: task brief §api-wired — 'without auth → 401'.
    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await api_client.get(_ADMIN_CONF)
    assert resp.status_code == 401, (
        f"GET /admin/conf without auth must return 401; got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_get_conf_non_admin_group_returns_403(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /admin/conf with non-admin JWT returns 403.

    spec: task brief §api-wired — 'without admin group (non-admin JWT) → 403'.
    spec: API.md §Group-to-Route Access Control — /admin/* requires 'admin' group.
    """
    from src.api.auth.jwt import create_access_token

    dg_token, _ = create_access_token(
        subject="dg-only-user",
        groups=["dg"],
        email="dg@test.example.com",
    )
    resp = await api_client.get(
        _ADMIN_CONF,
        headers={"Authorization": f"Bearer {dg_token}"},
    )
    assert resp.status_code == 403, (
        f"GET /admin/conf with 'dg'-only JWT must return 403; got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_patch_conf_without_auth_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /admin/conf without Authorization header returns 401.

    spec: task brief §api-wired — 'without auth → 401'.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        json={"llm_model": "gpt-4o-mini"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_conf_non_admin_group_returns_403(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /admin/conf with non-admin JWT returns 403.

    spec: task brief §api-wired — 'without admin group (non-admin JWT) → 403'.
    """
    from src.api.auth.jwt import create_access_token

    de_token, _ = create_access_token(
        subject="de-only-user",
        groups=["de"],
        email="de@test.example.com",
    )
    resp = await api_client.patch(
        _ADMIN_CONF,
        json={"llm_model": "gpt-4o-mini"},
        headers={"Authorization": f"Bearer {de_token}"},
    )
    assert resp.status_code == 403


# ── 3. PATCH /admin/conf — partial update round-trip ─────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_partial_body_updates_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with partial body returns 200; subsequent GET reflects values.

    spec: task brief §api-wired — 'PATCH with a partial body returns 200 with
    updated values; a subsequent GET reflects them.'
    Inline JSON payloads per project convention (feedback_test_readability).
    """
    try:
        # PATCH two fields only.
        patch_resp = await api_client.patch(
            _ADMIN_CONF,
            headers=admin_headers,
            json={
                "ontogen_debate_max_turns": 6,
                "llm_model": "gpt-4o-mini",
            },
        )

        assert patch_resp.status_code == 200, (
            f"PATCH /admin/conf returned {patch_resp.status_code}: {patch_resp.text}"
        )
        patch_body = patch_resp.json()
        # Updated fields reflected in the PATCH response itself.
        assert patch_body["ontogen_debate_max_turns"] == 6, (
            "PATCH response must reflect the updated ontogen_debate_max_turns value. "
            "spec: task brief §api-wired."
        )
        assert patch_body["llm_model"] == "gpt-4o-mini", (
            "PATCH response must reflect the updated llm_model. "
            "spec: task brief §api-wired."
        )

        # Subsequent GET must also return the patched values.
        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["ontogen_debate_max_turns"] == 6, (
            "GET /admin/conf after PATCH must reflect ontogen_debate_max_turns=6. "
            "spec: task brief §api-wired — 'subsequent GET reflects them'."
        )
        assert get_body["llm_model"] == "gpt-4o-mini", (
            "GET /admin/conf after PATCH must reflect llm_model='gpt-4o-mini'. "
            "spec: task brief §api-wired — 'subsequent GET reflects them'."
        )

    finally:
        await _reset_to_defaults(api_client, admin_headers)


@pytest.mark.asyncio
async def test_patch_conf_only_updates_supplied_fields(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf updates only the supplied fields; others are unchanged.

    spec: task brief §Unit — 'patch_runtime_config applies only provided fields,
    leaves others at prior values.'
    """
    try:
        await _reset_to_defaults(api_client, admin_headers)

        # Patch only one field.
        await api_client.patch(
            _ADMIN_CONF,
            headers=admin_headers,
            json={"validation_score_n_intervals": 5},
        )

        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        body = get_resp.json()

        # Patched field updated.
        assert body["validation_score_n_intervals"] == 5

        # All other fields retain factory defaults.
        for field, expected in _EXPECTED_DEFAULTS.items():
            if field == "validation_score_n_intervals":
                continue
            assert body[field] == expected, (
                f"Field '{field}' must remain at factory default {expected!r} after partial "
                f"patch; got {body[field]!r}. spec: task brief §Unit — partial patch."
            )

    finally:
        await _reset_to_defaults(api_client, admin_headers)


# ── 4. PATCH /admin/conf — out-of-bounds → 422 ───────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_above_max_debate_turns_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with ontogen_debate_max_turns=11 returns 422.

    Pydantic enforces le=10 before the service layer is reached.

    spec: task brief §api-wired — 'PATCH with out-of-bounds value → 422'.
    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — ontogen_debate_max_turns le=10.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={"ontogen_debate_max_turns": 11},
    )
    assert resp.status_code == 422, (
        f"ontogen_debate_max_turns=11 exceeds le=10; expected 422 but got {resp.status_code}: "
        f"{resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_below_min_debate_turns_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with ontogen_debate_max_turns=1 returns 422.

    Pydantic enforces ge=2 before the service layer is reached.

    spec: task brief §Unit — 'ontogen_debate_max_turns=1 rejected'.
    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — ge=2.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={"ontogen_debate_max_turns": 1},
    )
    assert resp.status_code == 422, (
        f"ontogen_debate_max_turns=1 violates ge=2; expected 422 but got {resp.status_code}: "
        f"{resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_confidence_threshold_too_high_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with metagen_confidence_threshold=1.5 returns 422.

    spec: task brief §Unit — 'metagen_confidence_threshold=1.5 rejected'.
    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — le=1.0.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={"metagen_confidence_threshold": 1.5},
    )
    assert resp.status_code == 422, (
        f"metagen_confidence_threshold=1.5 exceeds le=1.0; expected 422 but got "
        f"{resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_validation_intervals_zero_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with validation_score_n_intervals=0 returns 422.

    spec: task brief §Unit — 'validation_score_n_intervals=0 rejected'.
    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — ge=1.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={"validation_score_n_intervals": 0},
    )
    assert resp.status_code == 422, (
        f"validation_score_n_intervals=0 violates ge=1; expected 422 but got "
        f"{resp.status_code}: {resp.text}"
    )


# ── 5. PATCH /internal/admin/conf ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_conf_valid_token_returns_200_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/conf with valid X-Internal-Token → 200; GET reflects it.

    spec: task brief §api-wired — 'PATCH /internal/admin/conf with valid
    X-Internal-Token → 200 and reflected by GET'.
    spec: API.md §Internal routes — valid token grants access.
    """
    try:
        await _reset_to_defaults(api_client, admin_headers)

        patch_resp = await api_client.patch(
            _INTERNAL_CONF,
            headers=internal_headers,
            json={"metagen_debate_max_turns": 8},
        )

        # 200 from the internal endpoint (or 503 if token unset — handled below).
        if patch_resp.status_code == 503:
            # spec: API.md §503 — INTERNAL_AUTH_NOT_CONFIGURED when token unset.
            body = patch_resp.json()
            assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED", (
                "503 response must carry INTERNAL_AUTH_NOT_CONFIGURED error_code "
                "when DATASPOKE_INTERNAL_TOKEN is unset."
            )
            pytest.skip("DATASPOKE_INTERNAL_TOKEN not configured in this environment — skipping")

        assert patch_resp.status_code == 200, (
            f"PATCH /internal/admin/conf returned {patch_resp.status_code}: {patch_resp.text}"
        )
        patch_body = patch_resp.json()
        assert patch_body["metagen_debate_max_turns"] == 8

        # GET via the admin endpoint must reflect the change.
        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["metagen_debate_max_turns"] == 8, (
            "GET /admin/conf after PATCH /internal/admin/conf must reflect the "
            "updated metagen_debate_max_turns value. "
            "spec: task brief §api-wired — 'reflected by GET'."
        )

    finally:
        await _reset_to_defaults(api_client, admin_headers)


@pytest.mark.asyncio
async def test_internal_patch_conf_missing_token_returns_401_or_503(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /internal/admin/conf without X-Internal-Token → 401 or 503.

    spec: task brief §api-wired — 'without/with wrong token → 401 (or 503 if
    INTERNAL_TOKEN unset — match how existing internal-route tests assert)'.
    spec: API.md §Internal routes — 401 for missing/wrong token, 503 if unset.
    spec: API.md §503 — INTERNAL_AUTH_NOT_CONFIGURED.
    """
    resp = await api_client.patch(
        _INTERNAL_CONF,
        json={"llm_model": "gpt-4o-mini"},
        # No X-Internal-Token header
    )
    # 401 when token is set server-side but omitted here; 503 when token unset.
    assert resp.status_code in (401, 503), (
        f"Missing X-Internal-Token must return 401 or 503; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Internal routes / §503 INTERNAL_AUTH_NOT_CONFIGURED."
    )


@pytest.mark.asyncio
async def test_internal_patch_conf_wrong_token_returns_401(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/conf with wrong X-Internal-Token → 401.

    spec: task brief §api-wired — 'with wrong token → 401'.
    spec: API.md §Internal routes — constant-time compare; mismatch → 401.
    """
    resp = await api_client.patch(
        _INTERNAL_CONF,
        json={"llm_model": "gpt-4o-mini"},
        headers={"X-Internal-Token": "definitely-wrong-token-xyzzy"},
    )
    # 401 when a token is configured server-side; 503 when token is unset.
    assert resp.status_code in (401, 503), (
        f"Wrong X-Internal-Token must return 401 (or 503 if token unset); "
        f"got {resp.status_code}: {resp.text}"
    )
