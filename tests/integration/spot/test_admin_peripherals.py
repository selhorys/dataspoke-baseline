"""Spot integration tests for the DB-backed peripheral configuration feature.

Routes under test:
  GET  /api/v1/admin/peripherals               — requires Admin role
  GET  /api/v1/admin/peripherals/datahub       — requires Admin role
  PATCH /api/v1/admin/peripherals/datahub      — requires Admin role
  GET  /api/v1/admin/peripherals/langfuse      — requires Admin role
  PATCH /api/v1/admin/peripherals/langfuse     — requires Admin role
  PATCH /internal/admin/peripherals/datahub   — requires X-Internal-Token
  PATCH /internal/admin/peripherals/langfuse  — requires X-Internal-Token

Concerns covered:

1. GET unconfigured peripheral → is_configured=False, all fields empty, token/secret_key="".

2. PATCH {gms_url, kafka_brokers, token} → 200; response has masked token="********",
   non-secret fields visible; subsequent GET returns same masked values.

3. GET-after-PATCH: non-secret fields match what was sent; token="********".

4. PATCH {token: ""} clears the K8s Secret → subsequent GET shows is_configured=False,
   token="" (secret cleared).

5. PATCH {token: "x"} only from no-row state: K8s Secret written; peripheral_config
   table has 0 rows for 'datahub' (proves F6 empty-DB-write guard end-to-end).

6. Internal-token variant: PATCH /internal/admin/peripherals/datahub with
   X-Internal-Token → 200, reflected by GET.

7. Idempotent PATCH: apply same body twice, both return 200.

8. Non-admin user PATCH → 403.

9. Missing X-Internal-Token → 401 or 503 (env-dependent).

10. Same coverage mirror for langfuse peripheral.

CACHE NOTE: get_peripheral_config has a 30s process cache.  All state mutations go
through PATCH endpoints, which call invalidate_peripheral_config_cache() internally.
Tests are ordered so subsequent GETs see the PATCH-written value without out-of-band
invalidation.

CLEANUP: Each test (or try/finally block) deletes the peripheral_config rows and
the K8s Secrets at setup AND teardown so tests are order-independent.

Spec traceability:
- plan/scalable-beaming-hamster.md §API surface — all endpoint shapes, masking,
  is_configured predicate, F6 empty-PATCH guard.
- spec/API.md §Access Control — Admin role required for /admin/*.
- spec/API.md §Internal routes — X-Internal-Token required.
- src/api/schemas/admin.py DatahubPeripheralResponse / LangfusePeripheralResponse.
- src/backend/admin/peripheral_service.py — patch_peripheral_config empty partial.
"""

import os
import subprocess
from collections.abc import Iterator

import httpx
import pytest

import uuid as _uuid

from src.backend.auth.tokens import issue_access_token as _issue_access_token

# Module-level reset constants — this file only touches peripheral_config and
# K8s Secrets; no DataHub/Postgres dummy data needed.
# TESTING.md §Per-Module Dummy-Data Reset — omitting constants = no-op.

_ADMIN_PERIPHERALS = "/api/v1/admin/peripherals"
_ADMIN_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"
_ADMIN_PERIPHERALS_LF = "/api/v1/admin/peripherals/langfuse"
_INTERNAL_PERIPHERALS_DH = "/internal/admin/peripherals/datahub"
_INTERNAL_PERIPHERALS_LF = "/internal/admin/peripherals/langfuse"


# ── Cleanup helpers ───────────────────────────────────────────────────────────


def _db_delete_peripheral_rows() -> None:
    """Remove both datahub and langfuse rows from peripheral_config via psql.

    Runs synchronously; safe to call from fixtures and teardown blocks.
    """
    host = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
    port = os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")
    user = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
    password = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
    db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")

    sql = "DELETE FROM dataspoke.peripheral_config WHERE name IN ('datahub', 'langfuse');"
    env = {**os.environ, "PGPASSWORD": password}
    subprocess.run(
        ["psql", f"--host={host}", f"--port={port}", f"--username={user}", f"--dbname={db}", f"--command={sql}"],
        env=env,
        check=False,
        capture_output=True,
    )


def _k8s_delete_peripheral_secrets() -> None:
    """Delete the two K8s Secrets used by peripherals, ignoring errors if absent."""
    namespace = os.environ.get("DATASPOKE_KUBE_DATASPOKE_NAMESPACE", "dataspoke-01")
    for secret_name in ("dataspoke-datahub-secret", "dataspoke-langfuse-secret"):
        subprocess.run(
            ["kubectl", "delete", "secret", secret_name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
        )


def _reset_peripheral_state() -> None:
    """Full reset: remove DB rows AND K8s Secrets for both peripherals."""
    _db_delete_peripheral_rows()
    _k8s_delete_peripheral_secrets()


def _non_admin_headers() -> dict[str, str]:
    """Authorization headers for a non-existent user (unknown UUID sub).

    The in-cluster privilege layer cannot resolve this UUID to a DB user, so it
    returns 403 — which is the intended outcome for non-admin 403 assertions.
    Wave F will replace this with a properly seeded non-Admin user.
    """
    fake_id = _uuid.UUID("ffffffff-0000-0000-0000-000000000099")
    token, _ = _issue_access_token(fake_id, "non-admin@test.example.com")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module", autouse=True)
def _restore_dev_baseline_after_module() -> Iterator[None]:
    """Restore the dev-env peripheral baseline after this module's tests finish.

    Every test in this file calls ``_reset_peripheral_state()`` which deletes
    both peripheral_config rows and both K8s Secrets. Without this fixture,
    subsequent api-wired modules (UC1-UC5) would fail with 503 because the API
    sees no configured DataHub peripheral.
    """
    yield
    base = os.environ.get("DATASPOKE_KUBE_INGRESS_DOMAIN")
    token = os.environ.get("DATASPOKE_TEST_INTERNAL_TOKEN", "")
    dh_gms = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    dh_kafka = os.environ.get("DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS", "")
    dh_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")
    lf_host = os.environ.get("DATASPOKE_TEST_LANGFUSE_HOST", "")
    lf_pk = os.environ.get("DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY", "")
    lf_sk = os.environ.get("DATASPOKE_TEST_LANGFUSE_SECRET_KEY", "")
    if not (base and token and dh_gms and dh_token and lf_host and lf_sk):
        return
    headers = {"X-Internal-Token": token, "Content-Type": "application/json"}
    with httpx.Client(timeout=10.0) as client:
        client.patch(
            f"http://app.{base}/internal/admin/peripherals/datahub",
            headers=headers,
            json={"gms_url": dh_gms, "kafka_brokers": dh_kafka, "token": dh_token},
        )
        client.patch(
            f"http://app.{base}/internal/admin/peripherals/langfuse",
            headers=headers,
            json={"host": lf_host, "public_key": lf_pk, "secret_key": lf_sk},
        )


# ── 1. GET unconfigured — is_configured=False, fields empty ──────────────────


@pytest.mark.skip(
    reason="API caches DTO 30s + token 60s; deleting DB row + K8s Secret can't be "
    "observed within those windows from outside the API process. "
    "Contract covered by tests/unit/api/routers/test_admin_peripherals_routes.py "
    "(test_get_*_is_configured_false_*)."
)
@pytest.mark.asyncio
async def test_get_datahub_peripheral_unconfigured_returns_empty_fields(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/peripherals/datahub on an unconfigured peripheral returns empty fields.

    Both the DB row and the K8s Secret are absent. The response must have
    is_configured=False and all fields empty ("").

    spec: plan/scalable-beaming-hamster.md §API surface — unconfigured state.
    spec: src/api/routers/admin.py _datahub_dto_to_response — None dto → all empty.
    """
    _reset_peripheral_state()

    resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)

    assert resp.status_code == 200, (
        f"GET unconfigured datahub peripheral returned {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["is_configured"] is False, (
        f"is_configured must be False when no DB row and no K8s Secret; got {body!r}. "
        "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
    )
    assert body["gms_url"] == "", (
        f"gms_url must be '' when unconfigured; got {body['gms_url']!r}."
    )
    assert body["kafka_brokers"] == "", (
        f"kafka_brokers must be '' when unconfigured; got {body['kafka_brokers']!r}."
    )
    assert body["token"] == "", (
        f"token must be '' when unconfigured (no secret); got {body['token']!r}."
    )


@pytest.mark.skip(
    reason="See sibling test_get_datahub_peripheral_unconfigured_returns_empty_fields. "
    "Cache TTL window blocks observation from outside the API process. "
    "Contract covered by unit tests."
)
@pytest.mark.asyncio
async def test_get_langfuse_peripheral_unconfigured_returns_empty_fields(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/peripherals/langfuse on an unconfigured peripheral returns empty fields.

    spec: plan/scalable-beaming-hamster.md §API surface — unconfigured state.
    spec: src/api/routers/admin.py _langfuse_dto_to_response — None dto → all empty.
    """
    _reset_peripheral_state()

    resp = await api_client.get(_ADMIN_PERIPHERALS_LF, headers=admin_headers)

    assert resp.status_code == 200, (
        f"GET unconfigured langfuse peripheral returned {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["is_configured"] is False
    assert body["host"] == ""
    assert body["public_key"] == ""
    assert body["secret_key"] == ""


# ── 2. PATCH + 3. GET-after-PATCH — round-trip ────────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_and_get_reflects_values(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/peripherals/datahub → 200 with masked token; GET-after-PATCH reflects values.

    Step 1: PATCH with {gms_url, kafka_brokers, token}.
    Step 2: Assert PATCH response has token="********" (never plaintext).
    Step 3: GET — assert gms_url, kafka_brokers are preserved; token="********".
    Step 4: Assert is_configured=True.

    spec: plan/scalable-beaming-hamster.md §API surface — PATCH round-trip,
    token masking, is_configured=True after full config.
    """
    _reset_peripheral_state()
    try:
        # Step 1: PATCH full datahub config.
        patch_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={
                "gms_url": "http://datahub-test-gms:8080",
                "kafka_brokers": "datahub-test-kafka:9092",
                "token": "test-datahub-token-abc",
            },
        )

        assert patch_resp.status_code == 200, (
            f"PATCH /admin/peripherals/datahub returned {patch_resp.status_code}: {patch_resp.text}"
        )
        patch_body = patch_resp.json()

        # Step 2: token must be masked in the PATCH response.
        assert patch_body["token"] == "********", (
            f"PATCH response must mask token as '********'; got {patch_body['token']!r}. "
            "spec: plan/scalable-beaming-hamster.md §API surface — token masking."
        )
        assert "test-datahub-token-abc" not in str(patch_body), (
            "Plaintext token must never appear in the PATCH response. "
            "spec: plan/scalable-beaming-hamster.md §API surface."
        )
        assert patch_body["gms_url"] == "http://datahub-test-gms:8080"
        assert patch_body["kafka_brokers"] == "datahub-test-kafka:9092"

        # Step 3 & 4: GET-after-PATCH.
        get_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()

        assert get_body["gms_url"] == "http://datahub-test-gms:8080", (
            f"GET after PATCH must reflect gms_url; got {get_body['gms_url']!r}."
        )
        assert get_body["kafka_brokers"] == "datahub-test-kafka:9092", (
            f"GET after PATCH must reflect kafka_brokers; got {get_body['kafka_brokers']!r}."
        )
        assert get_body["token"] == "********", (
            f"GET after PATCH must return token='********' (set, masked); got {get_body['token']!r}."
        )
        assert get_body["is_configured"] is True, (
            f"is_configured must be True after full PATCH; got {get_body['is_configured']!r}. "
            "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
        )

    finally:
        _reset_peripheral_state()


@pytest.mark.asyncio
async def test_patch_langfuse_and_get_reflects_values(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/peripherals/langfuse → 200 with masked secret_key; GET reflects values.

    spec: plan/scalable-beaming-hamster.md §API surface — PATCH round-trip,
    secret_key masking, is_configured=True after full config.
    """
    _reset_peripheral_state()
    try:
        patch_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_LF,
            headers=admin_headers,
            json={
                "host": "http://langfuse-test:3000",
                "public_key": "pk-test-key",
                "secret_key": "sk-test-lf-secret",
            },
        )

        assert patch_resp.status_code == 200, (
            f"PATCH /admin/peripherals/langfuse returned {patch_resp.status_code}: {patch_resp.text}"
        )
        patch_body = patch_resp.json()

        assert patch_body["secret_key"] == "********", (
            f"PATCH response must mask secret_key as '********'; got {patch_body['secret_key']!r}."
        )
        assert "sk-test-lf-secret" not in str(patch_body), (
            "Plaintext secret_key must never appear in the PATCH response."
        )
        assert patch_body["host"] == "http://langfuse-test:3000"
        assert patch_body["public_key"] == "pk-test-key"

        get_resp = await api_client.get(_ADMIN_PERIPHERALS_LF, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()

        assert get_body["host"] == "http://langfuse-test:3000"
        assert get_body["public_key"] == "pk-test-key"
        assert get_body["secret_key"] == "********"
        assert get_body["is_configured"] is True

    finally:
        _reset_peripheral_state()


# ── 4. PATCH {token: ""} clears secret → GET shows is_configured=False ────────


@pytest.mark.asyncio
async def test_patch_datahub_clear_token_makes_is_configured_false(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH {token: ''} clears the K8s Secret; subsequent GET shows is_configured=False, token=''.

    spec: plan/scalable-beaming-hamster.md §API surface — token="" clears secret.
    spec: src/api/routers/admin.py _apply_datahub_patch_and_respond — explicit "" clears.
    """
    _reset_peripheral_state()
    try:
        # Set a token first.
        await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={
                "gms_url": "http://datahub-gms:8080",
                "kafka_brokers": "kafka:9092",
                "token": "initial-token-to-clear",
            },
        )

        # Now clear the token.
        clear_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"token": ""},
        )

        assert clear_resp.status_code == 200, (
            f"PATCH with token='' returned {clear_resp.status_code}: {clear_resp.text}"
        )
        clear_body = clear_resp.json()
        assert clear_body["token"] == "", (
            f"PATCH response with token='' must return token=''; got {clear_body['token']!r}. "
            "spec: plan/scalable-beaming-hamster.md §API surface."
        )

        # GET must reflect is_configured=False (secret cleared) even though DB row still exists.
        get_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()

        assert get_body["token"] == "", (
            f"GET after clearing token must return token=''; got {get_body['token']!r}."
        )
        assert get_body["is_configured"] is False, (
            f"is_configured must be False after token cleared; got {get_body['is_configured']!r}. "
            "spec: plan/scalable-beaming-hamster.md §is_configured predicate — "
            "secret unset → is_configured=False even if DB row exists."
        )

    finally:
        _reset_peripheral_state()


@pytest.mark.asyncio
async def test_patch_langfuse_clear_secret_key_makes_is_configured_false(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH {secret_key: ''} clears the K8s Secret → GET shows is_configured=False.

    spec: plan/scalable-beaming-hamster.md §API surface — secret_key="" clears secret.
    """
    _reset_peripheral_state()
    try:
        await api_client.patch(
            _ADMIN_PERIPHERALS_LF,
            headers=admin_headers,
            json={
                "host": "http://langfuse:3000",
                "public_key": "pk-test",
                "secret_key": "sk-initial",
            },
        )

        clear_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_LF,
            headers=admin_headers,
            json={"secret_key": ""},
        )

        assert clear_resp.status_code == 200
        clear_body = clear_resp.json()
        assert clear_body["secret_key"] == ""

        get_resp = await api_client.get(_ADMIN_PERIPHERALS_LF, headers=admin_headers)
        get_body = get_resp.json()

        assert get_body["secret_key"] == ""
        assert get_body["is_configured"] is False, (
            f"is_configured must be False after secret_key cleared; got {get_body['is_configured']!r}. "
            "spec: plan/scalable-beaming-hamster.md §is_configured predicate."
        )

    finally:
        _reset_peripheral_state()


# ── 5. PATCH {token: "x"} only from no-row state — F6 empty-DB-write guard ───


@pytest.mark.asyncio
async def test_patch_datahub_token_only_does_not_create_db_row(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH {token: 'x'} only: K8s Secret written; no peripheral_config row created.

    Starting from a completely clean state (no DB row, no K8s Secret),
    a token-only PATCH should:
    - Write the token to the K8s Secret.
    - NOT create a peripheral_config row (empty db_updates → no-op DB write).

    This proves the F6 empty-PATCH guard end-to-end.

    spec: plan/scalable-beaming-hamster.md §Backend — F6: empty partial no-op.
    spec: src/backend/admin/peripheral_service.py patch_peripheral_config —
    empty partial returns None without creating a row.
    """
    import asyncpg

    _reset_peripheral_state()
    try:
        # PATCH token only (no DB fields).
        patch_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"token": "x"},
        )

        assert patch_resp.status_code == 200, (
            f"Token-only PATCH returned {patch_resp.status_code}: {patch_resp.text}"
        )

        # Query the DB directly: peripheral_config must have 0 rows for 'datahub'.
        host = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201"))
        user = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
        password = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
        db_name = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")

        conn = await asyncpg.connect(
            host=host, port=port, user=user, password=password, database=db_name
        )
        try:
            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM peripheral_config WHERE name = 'datahub'"
            )
        finally:
            await conn.close()

        assert row_count == 0, (
            f"peripheral_config must have 0 rows for 'datahub' after token-only PATCH; "
            f"got {row_count}. "
            "spec: plan/scalable-beaming-hamster.md §Backend — F6: empty partial no-op. "
            "Token-only PATCH must not create a DB row."
        )

    finally:
        _reset_peripheral_state()


# ── 6. Internal-token variant ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_datahub_valid_token_returns_200_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/peripherals/datahub with valid X-Internal-Token → 200.

    The internal PATCH must write to DB and be reflected by the admin GET.

    spec: plan/scalable-beaming-hamster.md §API surface — internal-token variant.
    spec: API.md §Internal routes — X-Internal-Token required.
    """
    _reset_peripheral_state()
    try:
        patch_resp = await api_client.patch(
            _INTERNAL_PERIPHERALS_DH,
            headers=internal_headers,
            json={
                "gms_url": "http://internal-gms:8080",
                "kafka_brokers": "internal-kafka:9092",
            },
        )

        if patch_resp.status_code == 503:
            body = patch_resp.json()
            assert body.get("detail", {}).get("error_code") == "INTERNAL_AUTH_NOT_CONFIGURED"
            pytest.skip(
                "DATASPOKE_TEST_INTERNAL_TOKEN not configured in this environment — skipping"
            )

        assert patch_resp.status_code == 200, (
            f"PATCH /internal/admin/peripherals/datahub returned "
            f"{patch_resp.status_code}: {patch_resp.text}"
        )

        get_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()

        assert get_body["gms_url"] == "http://internal-gms:8080", (
            f"GET after internal PATCH must reflect gms_url; got {get_body['gms_url']!r}. "
            "spec: plan/scalable-beaming-hamster.md §API surface."
        )
        assert get_body["kafka_brokers"] == "internal-kafka:9092"

    finally:
        _reset_peripheral_state()


@pytest.mark.asyncio
async def test_internal_patch_langfuse_valid_token_returns_200_and_get_reflects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH /internal/admin/peripherals/langfuse with valid X-Internal-Token → 200.

    spec: plan/scalable-beaming-hamster.md §API surface — internal-token variant.
    """
    _reset_peripheral_state()
    try:
        patch_resp = await api_client.patch(
            _INTERNAL_PERIPHERALS_LF,
            headers=internal_headers,
            json={
                "host": "http://internal-lf:3000",
                "public_key": "internal-pk",
            },
        )

        if patch_resp.status_code == 503:
            pytest.skip(
                "DATASPOKE_TEST_INTERNAL_TOKEN not configured in this environment — skipping"
            )

        assert patch_resp.status_code == 200

        get_resp = await api_client.get(_ADMIN_PERIPHERALS_LF, headers=admin_headers)
        get_body = get_resp.json()

        assert get_body["host"] == "http://internal-lf:3000"
        assert get_body["public_key"] == "internal-pk"

    finally:
        _reset_peripheral_state()


# ── 7. Idempotent PATCH ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_idempotent(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Applying the same PATCH body twice to /admin/peripherals/datahub both return 200.

    spec: plan/scalable-beaming-hamster.md §API surface — idempotent PATCH.
    """
    _reset_peripheral_state()
    try:
        payload = {
            "gms_url": "http://idempotent-gms:8080",
            "kafka_brokers": "idempotent-kafka:9092",
            "token": "idempotent-token-xyz",
        }

        first_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH, headers=admin_headers, json=payload
        )
        assert first_resp.status_code == 200, (
            f"First PATCH returned {first_resp.status_code}: {first_resp.text}"
        )

        second_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH, headers=admin_headers, json=payload
        )
        assert second_resp.status_code == 200, (
            f"Second (idempotent) PATCH returned {second_resp.status_code}: {second_resp.text}. "
            "spec: plan/scalable-beaming-hamster.md §API surface — idempotent PATCH."
        )

    finally:
        _reset_peripheral_state()


@pytest.mark.asyncio
async def test_patch_langfuse_idempotent(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Applying the same PATCH body twice to /admin/peripherals/langfuse both return 200.

    spec: plan/scalable-beaming-hamster.md §API surface — idempotent PATCH.
    """
    _reset_peripheral_state()
    try:
        payload = {
            "host": "http://idempotent-lf:3000",
            "public_key": "pk-idempotent",
            "secret_key": "sk-idempotent",
        }

        first_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_LF, headers=admin_headers, json=payload
        )
        assert first_resp.status_code == 200

        second_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_LF, headers=admin_headers, json=payload
        )
        assert second_resp.status_code == 200, (
            f"Second (idempotent) PATCH returned {second_resp.status_code}: {second_resp.text}."
        )

    finally:
        _reset_peripheral_state()


# ── 8. Non-admin user PATCH → 403 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_datahub_non_admin_returns_403(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /admin/peripherals/datahub by non-admin user → 403.

    spec: spec/API.md §Access Control — Admin role required for /admin/*.
    """
    resp = await api_client.patch(
        _ADMIN_PERIPHERALS_DH,
        headers=_non_admin_headers(),
        json={"gms_url": "http://gms:8080"},
    )
    assert resp.status_code == 403, (
        f"Non-admin PATCH must return 403; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Admin routes."
    )


@pytest.mark.asyncio
async def test_patch_langfuse_non_admin_returns_403(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /admin/peripherals/langfuse by non-admin user → 403.

    spec: spec/API.md §Access Control — Admin role required for /admin/*.
    """
    resp = await api_client.patch(
        _ADMIN_PERIPHERALS_LF,
        headers=_non_admin_headers(),
        json={"host": "http://langfuse:3000"},
    )
    assert resp.status_code == 403


# ── 9. Missing X-Internal-Token → 401 or 503 ────────────────────────────────


@pytest.mark.asyncio
async def test_internal_patch_datahub_missing_token_returns_401_or_503(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /internal/admin/peripherals/datahub without X-Internal-Token → 401 or 503.

    401 when server has DATASPOKE_TEST_INTERNAL_TOKEN set but client omitted the header;
    503 (INTERNAL_AUTH_NOT_CONFIGURED) when token unset on the server side.

    spec: API.md §Internal routes — missing header → 401.
    spec: API.md §503 — INTERNAL_AUTH_NOT_CONFIGURED when token unset.
    """
    resp = await api_client.patch(
        _INTERNAL_PERIPHERALS_DH,
        json={"gms_url": "http://gms:8080"},
        # No X-Internal-Token header
    )
    assert resp.status_code in (401, 503), (
        f"Missing X-Internal-Token must return 401 or 503; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: API.md §Internal routes / §503 INTERNAL_AUTH_NOT_CONFIGURED."
    )


@pytest.mark.asyncio
async def test_internal_patch_langfuse_missing_token_returns_401_or_503(
    api_client: httpx.AsyncClient,
) -> None:
    """PATCH /internal/admin/peripherals/langfuse without X-Internal-Token → 401 or 503.

    spec: API.md §Internal routes — missing header → 401.
    """
    resp = await api_client.patch(
        _INTERNAL_PERIPHERALS_LF,
        json={"host": "http://langfuse:3000"},
    )
    assert resp.status_code in (401, 503)
