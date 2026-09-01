"""Spot integration tests for the DB-backed runtime configuration feature.

Routes under test:
  GET  /api/v1/admin/conf            — requires Admin role
  PATCH /api/v1/admin/conf           — requires Admin role
  PATCH /internal/admin/conf         — requires X-Internal-Token

Concerns covered:

1. GET /admin/conf on a fresh (or reset) DB returns all 15 behavioral tunables at factory
   defaults, includes resp_time and updated_at, and contains NO secret field.

2. PATCH /admin/conf with a partial body returns 200 with updated values;
   a subsequent GET reflects them.

3. PATCH /admin/conf with out-of-bounds value → 422.

4. Auth: GET/PATCH without JWT → 401; with non-Admin role → 403.

5. PATCH /internal/admin/conf with valid X-Internal-Token → 200, reflected by
   GET; without / with wrong token → 401 (or 503 if token unset).

6. PATCH /admin/conf (and /internal/admin/conf) with an unrecognised / misspelled
   field → 422 INVALID_PARAMETER (not a silent no-op); a rejected body writes
   nothing; the rejected value is never echoed; a bad-shape auth_datahub_corp_group
   → 422.

IMPORTANT CACHE NOTE: get_runtime_config has a ~30s process cache.  All state
mutations in this file go through the PATCH endpoint, which calls
invalidate_runtime_config_cache() internally before returning.  Tests are
therefore ordered so that each subsequent GET sees the value written by the
preceding PATCH without needing an out-of-band invalidation.

After each mutating test, the test restores the factory defaults via another
PATCH so later tests start from a known baseline.  The cache is automatically
warmed with the restored values by that cleanup PATCH.

Spec traceability:
- spec/API.md §Admin (/admin) — the /admin/conf surface: behavioral tunables +
  updated_at, partial PATCH, bound-validated numerics (out-of-range → 422),
  llm_api_key masked on GET. Defaults are explicitly left to impl there.
- spec/feature/BACKEND_LLM.md §Runtime configuration — the Field/Default/Bounds/Owner
  table whose 15 rows are the behavioral tunables _EXPECTED_DEFAULTS pins.
- spec/API.md §Access Control — Admin role required for /admin/*
- spec/API.md §Internal Admin (/internal/admin) — X-Internal-Token for /internal/…
- spec/API.md §Standard Response Envelope — resp_time on every response.

Impl traceability (the values and exact bounds the spec delegates to code):
- src/backend/admin/config_service.py RUNTIME_CONFIG_DEFAULTS — factory default values.
- src/api/schemas/admin.py RuntimeConfResponse / RuntimeConfPatchRequest — response
  shape and the numeric bounds enforced as 422.
"""

import httpx
import pytest

# Module-level constants — no DataHub/Postgres/Kafka data needed; the
# runtime_config table is managed by alembic and always present in the
# dev-env DB (seeded with factory defaults by the first GET).
# TESTING.md §Per-Module Dummy-Data Reset — omitting constants = no-op.

_ADMIN_CONF = "/api/v1/admin/conf"
_INTERNAL_CONF = "/internal/admin/conf"


# ── Factory default values used in assertions ─────────────────────────────────
# These are the factory-default values pinned to the impl SSOT
# (src/backend/admin/config_service.py RUNTIME_CONFIG_DEFAULTS). They are
# asserted as constants — NOT derived from the live API response — so the test
# fails if a default drifts. (API.md §Admin documents the /admin/conf tunables
# but not their default values, so the values themselves are an impl contract.)

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
    # spec'd default: BACKEND_LLM.md §Runtime configuration (row 15),
    # API.md §/admin/conf ("bounded, URN-safe token, default dataspoke-users"),
    # AUTH.md §Marker corpGroup ("Default name | dataspoke-users").
    "auth_datahub_corp_group": "dataspoke-users",
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
            "auth_datahub_corp_group": "dataspoke-users",
        },
    )
    assert resp.status_code == 200, f"Reset-to-defaults PATCH failed: {resp.text}"


# ── 1. GET /admin/conf — factory defaults, envelope, no-secret ───────────────


@pytest.mark.asyncio
async def test_get_conf_returns_factory_defaults(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf returns all 15 behavioral tunables at factory defaults.

    After reset to factory defaults the response values MUST match the
    documented factory defaults exactly.

    spec: API.md §Admin (/admin) — `GET /admin/conf` returns "runtime config (behavioral
    tunables + `updated_at`)"; the surface "is seeded with factory defaults and persisted
    in the `runtime_config` table … defaults live in impl, not here".
    spec: BACKEND_LLM.md §Runtime configuration — the Field/Default/Bounds/Owner table
    enumerates the behavioral tunables; its 15 rows are the set _EXPECTED_DEFAULTS pins.
    impl: src/backend/admin/config_service.py RUNTIME_CONFIG_DEFAULTS — the default
    *values*, which API.md explicitly leaves to impl.
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
            f"spec: src/api/schemas/admin.py RuntimeConfResponse / config_service.py "
            f"RUNTIME_CONFIG_DEFAULTS — factory default for {field}."
        )


@pytest.mark.asyncio
async def test_get_conf_includes_resp_time_and_updated_at(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf includes resp_time and updated_at in the response.

    spec: API.md §Admin (/admin) — `GET /admin/conf` returns "runtime config (behavioral
    tunables + `updated_at`)".
    spec: API.md §Standard Response Envelope — resp_time on every response.
    impl: src/api/schemas/admin.py RuntimeConfResponse — carries the tunables plus
    `updated_at`, and inherits `resp_time` from SingleResponse.
    """
    resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "resp_time" in body, (
        "resp_time must be present (spec: API.md §Standard Response Envelope)"
    )
    assert "updated_at" in body, (
        "updated_at must be present (spec: src/api/schemas/admin.py RuntimeConfResponse "
        "/ config_service.py RUNTIME_CONFIG_DEFAULTS)"
    )


@pytest.mark.asyncio
async def test_get_conf_masks_llm_api_key_and_exposes_no_other_secret(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/conf returns llm_api_key masked and exposes no other secret-named key.

    spec: src/api/schemas/admin.py RuntimeConfResponse — 'llm_api_key is a masked
      indicator only: "" when unset, "********" when set. The plaintext key is
      never returned.'
    spec: spec/API.md §/admin/conf — 'GET returns it masked ("" unset / "********"
      set) and never returns the plaintext.'
    spec: spec/feature/BACKEND_LLM.md §LLM API key — same masking contract.
    """
    resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()

    # llm_api_key must be present and masked — never plaintext.
    assert "llm_api_key" in body, (
        "llm_api_key must be present in the response (masked). "
        "spec: src/api/schemas/admin.py RuntimeConfResponse."
    )
    assert body["llm_api_key"] in ("", "********"), (
        f"llm_api_key must be masked (\"\" unset / \"********\" set); got "
        f"{body['llm_api_key']!r}. "
        "spec: spec/API.md §/admin/conf — never returns the plaintext."
    )

    # No OTHER secret-named key may appear — only the schema-mandated masked
    # llm_api_key. 'password' / 'secret' substrings, or any other 'api_key'
    # field, would indicate an unintended leak surface.
    other_secret_like = [
        k for k in body
        if k != "llm_api_key" and any(kw in k for kw in ("api_key", "secret", "password"))
    ]
    assert not other_secret_like, (
        f"Response must not expose secret-named fields beyond the masked "
        f"llm_api_key; found: {other_secret_like}. "
        "spec: src/api/schemas/admin.py RuntimeConfResponse — only llm_api_key "
        "is masked-exposed; no other secret surface."
    )


# ── 2. Auth: GET/PATCH require Admin role ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_auth_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /admin/conf without Authorization header returns 401.

    spec: API.md §Admin (/admin) — 'without auth → 401'.
    spec: API.md §Authentication — admin routes require valid JWT.
    """
    resp = await api_client.get(_ADMIN_CONF)
    assert resp.status_code == 401, (
        f"GET /admin/conf without auth must return 401; got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_get_conf_non_admin_role_returns_403(
    api_client: httpx.AsyncClient,
    async_session,
) -> None:
    """GET /admin/conf with a real Reader-role caller returns 403 FORBIDDEN.

    Uses a REAL seeded Reader user so the 403 comes from the require_admin role
    gate (the user IS authenticated; they simply lack Admin role).

    spec: API.md §Access Control — /admin/* requires users.role = 'Admin';
        Editor/Reader → 403 FORBIDDEN.
    spec: feature/AUTH.md §Privilege Model — /admin/* column: Reader ✗, Editor ✗.
    spec: feature/AUTH.md §Lifecycle §Deletion — deleted/unknown subject → 401,
        not 403; role gate only fires when the user EXISTS.
    """
    import uuid

    from sqlalchemy import text

    from src.backend.auth.tokens import issue_access_token

    reader_id = uuid.uuid4()
    reader_email = f"reader-conf-get-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Reader Test User",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()
    try:
        non_admin_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)
        resp = await api_client.get(
            _ADMIN_CONF,
            headers={"Authorization": f"Bearer {non_admin_token}"},
        )
        assert resp.status_code == 403, (
            f"Reader-role caller must return 403 FORBIDDEN on GET /admin/conf "
            f"per spec/API.md §Access Control; got {resp.status_code}: {resp.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(reader_id)},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_patch_conf_without_auth_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /admin/conf without Authorization header returns 401.

    spec: API.md §Admin (/admin) — 'without auth → 401'.
    """
    resp = await api_client.patch(
        _ADMIN_CONF,
        json={"llm_model": "gpt-4o-mini"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_conf_non_admin_role_returns_403(
    api_client: httpx.AsyncClient,
    async_session,
) -> None:
    """PATCH /admin/conf with a real Reader-role caller returns 403 FORBIDDEN.

    Uses a REAL seeded Reader user so the 403 comes from the require_admin role
    gate (the user IS authenticated; they simply lack Admin role).

    spec: API.md §Access Control — /admin/* requires users.role = 'Admin';
        Editor/Reader → 403 FORBIDDEN.
    spec: feature/AUTH.md §Privilege Model — /admin/* column: Reader ✗, Editor ✗.
    spec: feature/AUTH.md §Lifecycle §Deletion — deleted/unknown subject → 401,
        not 403; role gate only fires when the user EXISTS.
    """
    import uuid

    from sqlalchemy import text

    from src.backend.auth.tokens import issue_access_token

    reader_id = uuid.uuid4()
    reader_email = (
        f"reader-conf-patch-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    )
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Reader Test User",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()
    try:
        non_admin_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)
        resp = await api_client.patch(
            _ADMIN_CONF,
            json={"llm_model": "gpt-4o-mini"},
            headers={"Authorization": f"Bearer {non_admin_token}"},
        )
        assert resp.status_code == 403, (
            f"Reader-role caller must return 403 FORBIDDEN on PATCH /admin/conf "
            f"per spec/API.md §Access Control; got {resp.status_code}: {resp.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(reader_id)},
        )
        await async_session.commit()


# ── 3. PATCH /admin/conf — partial update round-trip ─────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_partial_body_updates_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with partial body returns 200; subsequent GET reflects values.

    spec: API.md §Admin (/admin) — 'PATCH with a partial body returns 200 with
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
            "spec: API.md §Admin (/admin)."
        )
        assert patch_body["llm_model"] == "gpt-4o-mini", (
            "PATCH response must reflect the updated llm_model. "
            "spec: API.md §Admin (/admin)."
        )

        # Subsequent GET must also return the patched values.
        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["ontogen_debate_max_turns"] == 6, (
            "GET /admin/conf after PATCH must reflect ontogen_debate_max_turns=6. "
            "spec: API.md §Admin (/admin) — 'subsequent GET reflects them'."
        )
        assert get_body["llm_model"] == "gpt-4o-mini", (
            "GET /admin/conf after PATCH must reflect llm_model='gpt-4o-mini'. "
            "spec: API.md §Admin (/admin) — 'subsequent GET reflects them'."
        )

    finally:
        await _reset_to_defaults(api_client, admin_headers)


@pytest.mark.asyncio
async def test_patch_conf_only_updates_supplied_fields(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf updates only the supplied fields; others are unchanged.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — 'patch_runtime_config applies only
    provided fields,
    leaves others at prior values.'
    """
    try:
        await _reset_to_defaults(api_client, admin_headers)

        # Patch only one field.
        await api_client.patch(
            _ADMIN_CONF,
            headers=admin_headers,
            json={"metagen_ontology_rag_node_k": 7},
        )

        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        body = get_resp.json()

        # Patched field updated.
        assert body["metagen_ontology_rag_node_k"] == 7

        # All other fields retain factory defaults.
        for field, expected in _EXPECTED_DEFAULTS.items():
            if field == "metagen_ontology_rag_node_k":
                continue
            assert body[field] == expected, (
                f"Field '{field}' must remain at factory default {expected!r} after partial "
                f"patch; got {body[field]!r}. spec: src/api/schemas/admin.py "
                f"RuntimeConfPatchRequest — partial patch."
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

    spec: API.md §Admin (/admin) — 'PATCH with out-of-bounds value → 422'.
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

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — 'ontogen_debate_max_turns=1 rejected'.
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

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — 'metagen_confidence_threshold=1.5
    rejected'.
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


# ── 5. PATCH /internal/admin/conf ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_conf_valid_token_returns_200_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/conf with valid X-Internal-Token → 200; GET reflects it.

    spec: API.md §Admin (/admin) — 'PATCH /internal/admin/conf with valid
    X-Internal-Token → 200 and reflected by GET'.
    spec: API.md §Internal Admin (/internal/admin) — valid token grants access.
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
            # spec: API.md §Application Error Codes — INTERNAL_AUTH_NOT_CONFIGURED when token unset.
            body = patch_resp.json()
            assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED", (
                "503 response must carry INTERNAL_AUTH_NOT_CONFIGURED error_code "
                "when DATASPOKE_DEV_INTERNAL_TOKEN is unset."
            )
            pytest.skip(
                "DATASPOKE_DEV_INTERNAL_TOKEN not configured in this environment — skipping"
            )

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
            "spec: API.md §Admin (/admin) — 'reflected by GET'."
        )

    finally:
        await _reset_to_defaults(api_client, admin_headers)


@pytest.mark.asyncio
async def test_internal_patch_conf_missing_token_returns_401_or_503(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /internal/admin/conf without X-Internal-Token → 401 or 503.

    spec: API.md §Admin (/admin) — 'without/with wrong token → 401 (or 503 if
    INTERNAL_TOKEN unset — match how existing internal-route tests assert)'.
    spec: API.md §Internal Admin (/internal/admin) — 401 for missing/wrong token, 503 if unset.
    spec: API.md §Application Error Codes — INTERNAL_AUTH_NOT_CONFIGURED.
    """
    resp = await api_client.patch(
        _INTERNAL_CONF,
        json={"llm_model": "gpt-4o-mini"},
        # No X-Internal-Token header
    )
    # 401 when token is set server-side but omitted here; 503 when token unset.
    assert resp.status_code in (401, 503), (
        f"Missing X-Internal-Token must return 401 or 503; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Internal Admin (/internal/admin) / §Application Error Codes "
        "(INTERNAL_AUTH_NOT_CONFIGURED)."
    )


@pytest.mark.asyncio
async def test_internal_patch_conf_wrong_token_returns_401(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/conf with wrong X-Internal-Token → 401.

    spec: API.md §Admin (/admin) — 'with wrong token → 401'.
    spec: API.md §Internal Admin (/internal/admin) — constant-time compare; mismatch → 401.
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


# ── 6. Unrecognised / misspelled field → 422 (not a silent no-op) ────────────


@pytest.mark.asyncio
async def test_patch_conf_only_key_unrecognised_returns_422_and_writes_nothing(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf whose ONLY key is unrecognised → 422; nothing is written.

    The rejected body must be a complete no-op — a subsequent GET shows every
    documented default unchanged.

    spec: spec/API.md §/admin/conf — "A PATCH body carrying an unrecognised field
    is rejected 422 INVALID_PARAMETER rather than silently ignored, so a
    misspelled toggle or knob name fails loudly instead of leaving the config
    unchanged."
    spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
    unknown write-body fields → 422 INVALID_PARAMETER, not a silent no-op.
    """
    try:
        await _reset_to_defaults(api_client, admin_headers)

        resp = await api_client.patch(
            _ADMIN_CONF,
            headers=admin_headers,
            json={"stub_llm_clients": False},  # real field is `stub_llm_client`
        )
        assert resp.status_code == 422, (
            f"an unrecognised key must return 422; got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["error_code"] == "INVALID_PARAMETER", (
            f"422 body must carry error_code INVALID_PARAMETER; got {resp.text}"
        )

        # Nothing was written — every documented default is still in place.
        get_resp = await api_client.get(_ADMIN_CONF, headers=admin_headers)
        assert get_resp.status_code == 200
        body = get_resp.json()
        for field, expected in _EXPECTED_DEFAULTS.items():
            assert body[field] == expected, (
                f"Field '{field}' changed after a rejected PATCH; expected {expected!r}, "
                f"got {body[field]!r}. The rejected body must be a no-op. "
                "spec: spec/API.md §/admin/conf."
            )
    finally:
        await _reset_to_defaults(api_client, admin_headers)


@pytest.mark.asyncio
async def test_internal_patch_conf_unrecognised_field_returns_422(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/conf with an unrecognised key → 422.

    /internal/admin/conf shares _apply_patch_and_respond with the public route,
    so the extra="forbid" contract holds there too.

    spec: spec/API.md §/admin/conf — an unrecognised field is rejected
    422 INVALID_PARAMETER rather than silently ignored.
    spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests).
    """
    resp = await api_client.patch(
        _INTERNAL_CONF,
        headers=internal_headers,
        json={"stub_llm_clients": False},  # real field is `stub_llm_client`
    )
    if resp.status_code == 503:
        # spec: API.md §Application Error Codes — INTERNAL_AUTH_NOT_CONFIGURED when token unset.
        body = resp.json()
        assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED", (
            "503 response must carry INTERNAL_AUTH_NOT_CONFIGURED when the internal token is unset."
        )
        pytest.skip(
            "DATASPOKE_DEV_INTERNAL_TOKEN not configured in this environment — skipping"
        )

    assert resp.status_code == 422, (
        f"an unrecognised key on /internal/admin/conf must return 422; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "INVALID_PARAMETER", (
        f"422 body must carry error_code INVALID_PARAMETER; got {resp.text}"
    )


@pytest.mark.asyncio
async def test_patch_conf_misspelled_llm_api_key_returns_422_without_echoing_value(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A misspelled secret key (`llm_apikey`) → 422, and the submitted value is
    never echoed anywhere in the response.

    spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
    the error envelope "never reproduces the offending value, because
    write-request bodies routinely carry credentials".
    spec: spec/API.md §Error Catalogue — "The rejected value is not echoed".
    """
    canary = "sk-NOTREAL-leak-canary-123"
    resp = await api_client.patch(
        _ADMIN_CONF,
        headers=admin_headers,
        json={"llm_apikey": canary},  # real field is `llm_api_key`
    )
    assert resp.status_code == 422, (
        f"misspelled llm_apikey must return 422; got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "INVALID_PARAMETER"
    assert canary not in resp.text, (
        "the rejected value must not appear anywhere in the 422 response "
        "per spec/API_DESIGN_PRINCIPLE_en.md §4"
    )


@pytest.mark.asyncio
async def test_patch_conf_invalid_corp_group_pattern_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/conf with a non-URN-safe auth_datahub_corp_group → 422.

    spec: spec/API.md §/admin/conf — auth_datahub_corp_group is "a bounded,
    URN-safe token"; "string fields are length- and shape-bound".
    spec: spec/feature/AUTH.md §Marker corpGroup — "Length-capped, URN-safe
    charset … interpolated into the group URN and displayName."
    """
    try:
        resp = await api_client.patch(
            _ADMIN_CONF,
            headers=admin_headers,
            json={"auth_datahub_corp_group": "bad name)"},
        )
        assert resp.status_code == 422, (
            f"a whitespace/paren corpGroup name must return 422; "
            f"got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["error_code"] == "INVALID_PARAMETER"
    finally:
        await _reset_to_defaults(api_client, admin_headers)
