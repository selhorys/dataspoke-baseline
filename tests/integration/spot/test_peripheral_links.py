"""Spot integration tests for the peripheral display-link surface.

Route under test:
  GET /api/v1/spoke/common/peripheral-links   — any authenticated role

Concerns covered:

1. Unauthenticated call → 401 (the route reads the DB, so a guard ordered after
   the session dependency would surface as 500 instead).
2. Admin PATCHes ``frontend_url`` via /admin/peripherals/datahub; a **Reader**
   reads it back from /spoke/common/peripheral-links. This is the whole point of
   the endpoint — /admin/* is Admin-gated, so a Reader cannot obtain these links
   from the admin surface, and the same Reader is asserted to be refused there.
3. ``datahub_url`` is sourced from ``frontend_url``, never ``gms_url``. The two
   are set to differ in host, port, AND scheme (mirroring the reported deployment
   where GMS is an internal plain-HTTP ELB and the UI a public TLS hostname), so a
   mis-wiring cannot coincidentally produce the expected value.
4. The payload carries only {datahub_url, langfuse_url, langfuse_project_id} plus
   the envelope's ``resp_time``: no ``gms_url``, ``kafka_brokers``, or
   ``service_corpuser_urn``. The excluded values are seeded first, so their
   absence is a real filter rather than a trivially-true assertion.
5. A ``peripheral_config`` row poisoned by **direct SQL** — bypassing the admin
   request schema entirely — is coerced to "" by the read boundary. This state is
   unreachable through the API (the PATCH schema rejects it), which is why it
   lives at spot rather than in an api-wired flow.

Why spot and not api-wired: `spec/TESTING.md §Spot vs Api-Wired Integration Tests`
reserves `api_wired/` for "the five `USE_CASE_en.md` user stories", one file per
UC named `test_uc{n}_{nn}_<slug>.py`. `USE_CASE_en.md` carries no peripheral-wiring
narrative, so this endpoint has no user story to mirror; it is a single REST
endpoint behavior, which is exactly the spot boundary ("a spot test may call
dataspoke Python directly **or** call the API over HTTP").

CACHE NOTE: `get_peripheral_config` keeps a 30s process-level cache. Mutations
made through PATCH invalidate it internally, so a PATCH→GET sequence observes the
new value. The direct-SQL poisoning in concern 5 does NOT invalidate it, so that
test forces an invalidation through a benign PATCH afterwards.

CLEANUP: every test snapshots the DataHub peripheral's non-secret fields first and
restores them in `finally`, asserting the restore (spec/TESTING.md §Integration
Lifecycle & Isolation — "The restore is **asserted**, not assumed"). The secret
(`token`) is never touched.

Spec traceability:
- spec/API.md §Data Resource — `GET /spoke/common/peripheral-links`: payload shape,
  `datahub_url` ⟵ `datahub.frontend_url` ("**never** `gms_url`"), ""-means-unset,
  any-authenticated-role gate, no-infrastructure-disclosure rule.
- spec/API.md §Access Control — /admin/* requires role=Admin.
- spec/feature/BACKEND_SCHEMA.md §peripheral_config — `frontend_url` is a
  non-secret DataHub field in the `settings` JSONB.
"""

import os
import subprocess
import uuid as _uuid
from collections.abc import Iterator

import httpx
import pytest

from src.backend.auth.tokens import issue_access_token as _issue_access_token

# This module touches only `peripheral_config` and one temporary `users` row; no
# DataHub/Postgres dummy data is needed.
# spec/TESTING.md §Per-Module Dummy-Data Reset — omitting the constants = no-op.

_PERIPHERAL_LINKS = "/api/v1/spoke/common/peripheral-links"
_ADMIN_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"

# Deliberately unlike any real dev-cluster value, and unlike each other in host,
# port, AND scheme — the reported ELB-vs-public-hostname shape.
_TEST_FRONTEND_URL = "https://datahub-ui.imazon-spot.example.com:8443"
_TEST_GMS_URL = "http://datahub-gms-elb.imazon-spot.internal:8080"

# Fields that must never appear on this non-Admin surface.
_FORBIDDEN_KEYS = ("gms_url", "kafka_brokers", "service_corpuser_urn", "default_env", "token")


# ── psql helpers ──────────────────────────────────────────────────────────────


def _psql(sql: str) -> subprocess.CompletedProcess[bytes]:
    """Run one SQL statement against the dev Postgres, failing loud on error.

    Credentials come from the ``DATASPOKE_TEST_*`` block in helm-charts/.env.dev.
    spec/TESTING.md §Integration Lifecycle & Isolation — "Reset helpers fail loud
    and carry no baked-in credentials".
    """
    env = {**os.environ, "PGPASSWORD": os.environ["DATASPOKE_TEST_POSTGRES_PASSWORD"]}
    return subprocess.run(
        [
            "psql",
            f"--host={os.environ['DATASPOKE_TEST_POSTGRES_HOST']}",
            f"--port={os.environ['DATASPOKE_TEST_POSTGRES_PORT']}",
            f"--username={os.environ['DATASPOKE_TEST_POSTGRES_USER']}",
            f"--dbname={os.environ['DATASPOKE_TEST_POSTGRES_DB']}",
            "--set=ON_ERROR_STOP=1",
            f"--command={sql}",
        ],
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def reader_headers() -> Iterator[dict[str, str]]:
    """Seed a real Reader-role user and yield its bearer header; delete it after.

    A real DB row is required: the role gate only fires for a user that exists
    (an unknown subject yields 401, not 403).

    spec: spec/API.md §Access Control — /admin/* requires role=Admin.
    """
    user_id = _uuid.uuid4()
    email = f"reader-links-{str(_uuid.uuid4())[:8]}@test.dataspoke.example.com"
    _psql(
        "INSERT INTO dataspoke.users (id, email, name, google_sub, role) VALUES "
        f"('{user_id}', '{email}', 'Peripheral Links Reader', "
        f"'test-sub-{_uuid.uuid4()}', 'Reader');"
    )
    token, _ = _issue_access_token(user_id, email, session_epoch=0)
    try:
        yield {"Authorization": f"Bearer {token}"}
    finally:
        _psql(f"DELETE FROM dataspoke.users WHERE id = '{user_id}';")


# ── 1. Auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthenticated_peripheral_links_read_is_401(
    api_client: httpx.AsyncClient,
) -> None:
    """A call with no bearer token is rejected as unauthenticated, not 500.

    The handler opens a DB session, so an auth guard ordered after the session
    dependency would surface as a 500. Pinned explicitly against a live DB.

    spec: spec/API.md §Data Resource — the route is "Readable by **any
        authenticated role**"; spec/API.md §Access Control.
    """
    resp = await api_client.get(_PERIPHERAL_LINKS)

    assert resp.status_code == 401, (
        f"Unauthenticated read must be 401, got {resp.status_code}: {resp.text}"
    )
    assert "datahub_url" not in resp.json(), "A rejected request must not leak the payload"


# ── 2-4. The core story: Admin writes, Reader reads ───────────────────────────


@pytest.mark.asyncio
async def test_admin_patches_frontend_url_and_reader_reads_it_back(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    reader_headers: dict[str, str],
) -> None:
    """Admin PATCHes frontend_url; a Reader resolves it from peripheral-links.

    Covers the endpoint's reason to exist (any-role read), its source-of-truth
    mapping (frontend_url, never gms_url), and its disclosure boundary (only the
    three display fields).

    spec: spec/API.md §Data Resource — "`datahub_url` ⟵ `datahub.frontend_url`
        (the browser-facing UI URL — **never** `gms_url` …)"; "Readable by **any
        authenticated role** (the `/admin/*` surface is Admin-only, so it cannot
        serve Readers and Editors)"; "Returns only these three display fields —
        no `gms_url`, `kafka_brokers`, or corpuser URN".
    """
    # Snapshot the non-secret DataHub fields so they can be restored exactly.
    snapshot_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    snapshot = snapshot_resp.json()

    try:
        # -- Admin sets a browser URL that differs from GMS in host, port, scheme --
        patch_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={
                "gms_url": _TEST_GMS_URL,
                "frontend_url": _TEST_FRONTEND_URL,
                "kafka_brokers": "kafka-spot.imazon-spot.internal:9092",
                "service_corpuser_urn": "urn:li:corpuser:imazon-spot-svc",
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["frontend_url"] == _TEST_FRONTEND_URL, (
            "The admin surface must echo the stored frontend_url"
        )

        # -- Backstop: the Reader genuinely cannot use the admin surface --
        # Without this the Reader read below would prove nothing about the gate.
        reader_admin_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=reader_headers)
        assert reader_admin_resp.status_code == 403, (
            f"A Reader must be refused by /admin/peripherals/datahub, got "
            f"{reader_admin_resp.status_code}: {reader_admin_resp.text}"
        )

        # -- The Reader resolves the link from the non-Admin surface --
        links_resp = await api_client.get(_PERIPHERAL_LINKS, headers=reader_headers)
        assert links_resp.status_code == 200, (
            f"A Reader must be able to read peripheral-links: {links_resp.text}"
        )
        body = links_resp.json()

        assert body["datahub_url"] == _TEST_FRONTEND_URL, (
            "datahub_url must be the browser-facing frontend_url. "
            "spec: spec/API.md §Data Resource — datahub_url ⟵ datahub.frontend_url."
        )
        assert body["datahub_url"] != _TEST_GMS_URL, (
            "datahub_url must never be sourced from gms_url — the two differ in "
            "host, port, and scheme in this test precisely to catch that mis-wiring."
        )

        # -- Disclosure boundary: exactly the three display fields --
        assert set(body) == {
            "datahub_url",
            "langfuse_url",
            "langfuse_project_id",
            "resp_time",
        }, f"Unexpected peripheral-links keys: {sorted(body)}"
        for key in _FORBIDDEN_KEYS:
            assert key not in body, f"{key} must not appear on this non-Admin surface"
        # The excluded values were seeded above, so these absence checks are real.
        assert _TEST_GMS_URL not in links_resp.text, "The GMS endpoint must not leak"
        assert "kafka-spot.imazon-spot.internal:9092" not in links_resp.text, (
            "Kafka brokers must not leak"
        )
        assert "urn:li:corpuser:imazon-spot-svc" not in links_resp.text, (
            "The service corpuser URN must not leak"
        )
    finally:
        restore_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={
                "gms_url": snapshot["gms_url"],
                "frontend_url": snapshot["frontend_url"],
                "kafka_brokers": snapshot["kafka_brokers"],
                "service_corpuser_urn": snapshot["service_corpuser_urn"],
            },
        )
        assert restore_resp.status_code == 200, f"restore failed: {restore_resp.text}"
        restored = restore_resp.json()
        assert restored["gms_url"] == snapshot["gms_url"], "gms_url was not restored"
        assert restored["frontend_url"] == snapshot["frontend_url"], (
            "frontend_url was not restored — later tests would run against a "
            "corrupted peripheral baseline"
        )
        assert restored["kafka_brokers"] == snapshot["kafka_brokers"]
        assert restored["service_corpuser_urn"] == snapshot["service_corpuser_urn"]


@pytest.mark.asyncio
async def test_admin_patch_rejects_an_unsafe_frontend_url(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The admin write boundary refuses a frontend_url that is not a safe http(s) URL.

    Proves the poisoned state exercised by the next test is genuinely unreachable
    through the API — which is why that test seeds it by direct SQL.

    spec: spec/API.md §Data Resource → Display-link safety — "On write,
        ``PATCH /admin/peripherals/{datahub,langfuse}`` rejects a violating value
        with ``422``."
    """
    for hostile in ("javascript:alert(1)", "//evil.example.com", "data:text/html,x"):
        resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"frontend_url": hostile},
        )
        assert resp.status_code == 422, (
            f"PATCH frontend_url={hostile!r} must be refused with 422, "
            f"got {resp.status_code}: {resp.text}"
        )


# ── 5. Poisoned row seeded by direct SQL ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "poisoned"),
    [
        ("javascript scheme", "javascript:alert(1)"),
        ("protocol-relative", "//evil.example.com"),
        ("userinfo spoofing", "https://datahub.imazon.example.com@evil.example.com"),
    ],
)
async def test_directly_seeded_hostile_frontend_url_is_coerced_to_empty(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    label: str,
    poisoned: str,
) -> None:
    """A hostile frontend_url written straight into the JSONB never reaches the response.

    ``peripheral_config.settings`` is untyped JSONB, so a row seeded by direct SQL
    (a migration, an operator's psql session, a future writer that skips the admin
    schema) can hold anything. The read boundary re-checks and degrades the value
    to "" — the documented "render no link" state — rather than forwarding it into
    a browser ``href``. The previous test shows this state cannot be produced
    through the API, which is why it is seeded here instead.

    spec: spec/API.md §Data Resource → Display-link safety — "On read,
        ``GET /spoke/common/peripheral-links`` coerces one to ``""``:
        ``peripheral_config.settings`` is JSONB, so a row written by direct SQL or
        by dev seeding can bypass the request schema."
    """
    snapshot_resp = await api_client.get(_ADMIN_PERIPHERALS_DH, headers=admin_headers)
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    snapshot = snapshot_resp.json()

    try:
        # Seed a known-good value through the API first, so the assertion below
        # distinguishes "coerced to empty" from "was empty all along".
        seed_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"frontend_url": _TEST_FRONTEND_URL},
        )
        assert seed_resp.status_code == 200, seed_resp.text
        before = await api_client.get(_PERIPHERAL_LINKS, headers=admin_headers)
        assert before.status_code == 200, before.text
        assert before.json()["datahub_url"] == _TEST_FRONTEND_URL, (
            "Precondition failed: the safe value must be visible before poisoning, "
            "otherwise the '' assertion below is vacuous."
        )

        # Poison the JSONB directly, bypassing the request schema entirely.
        escaped = poisoned.replace("'", "''")
        _psql(
            "UPDATE dataspoke.peripheral_config "
            f"SET settings = jsonb_set(settings, '{{frontend_url}}', to_jsonb('{escaped}'::text)) "
            "WHERE name = 'datahub';"
        )

        # A direct SQL write cannot invalidate the API's 30s DTO cache; a benign
        # PATCH on an unrelated field does, without overwriting the poisoned key.
        bump_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"default_env": snapshot["default_env"] or "DEV"},
        )
        assert bump_resp.status_code == 200, bump_resp.text
        assert bump_resp.json()["frontend_url"] == poisoned, (
            f"{label}: the poisoned value must actually be stored in the row — "
            "otherwise the read-boundary assertion below proves nothing."
        )

        after = await api_client.get(_PERIPHERAL_LINKS, headers=admin_headers)
        assert after.status_code == 200, f"{label}: poisoned row must not 500: {after.text}"
        assert after.json()["datahub_url"] == "", (
            f"{label}: a stored value that is not a safe http(s) URL must be coerced "
            f"to '' at the read boundary, never forwarded to a browser href."
        )
    finally:
        # The poisoned value cannot be overwritten through the PATCH schema, so
        # the restore goes back through SQL, then a PATCH to flush the cache.
        escaped_original = (snapshot["frontend_url"] or "").replace("'", "''")
        _psql(
            "UPDATE dataspoke.peripheral_config "
            "SET settings = jsonb_set(settings, '{frontend_url}', "
            f"to_jsonb('{escaped_original}'::text)) WHERE name = 'datahub';"
        )
        restore_resp = await api_client.patch(
            _ADMIN_PERIPHERALS_DH,
            headers=admin_headers,
            json={"default_env": snapshot["default_env"] or "DEV"},
        )
        assert restore_resp.status_code == 200, f"restore failed: {restore_resp.text}"
        assert restore_resp.json()["frontend_url"] == snapshot["frontend_url"], (
            "frontend_url was not restored — later tests would run against a "
            "corrupted peripheral baseline"
        )


# ── Langfuse mapping ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_langfuse_links_mirror_the_langfuse_peripheral(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """`langfuse_url` mirrors the Langfuse `host`; `langfuse_project_id` its `project_id`.

    spec: spec/API.md §Data Resource — "`langfuse_url` ⟵ `langfuse.host` (the
        Langfuse peripheral contract names this field `host`), `langfuse_project_id`
        ⟵ `langfuse.project_id`".
    """
    admin_lf = await api_client.get("/api/v1/admin/peripherals/langfuse", headers=admin_headers)
    assert admin_lf.status_code == 200, admin_lf.text
    lf = admin_lf.json()

    links = await api_client.get(_PERIPHERAL_LINKS, headers=admin_headers)
    assert links.status_code == 200, links.text
    body = links.json()

    assert body["langfuse_url"] == lf["host"], (
        "langfuse_url must mirror the Langfuse peripheral's host field"
    )
    assert body["langfuse_project_id"] == lf["project_id"], (
        "langfuse_project_id must mirror the Langfuse peripheral's project_id field"
    )
    # Backstop: the dev cluster seeds Langfuse, so this is not an ""=="" pass.
    assert lf["host"], (
        "The dev Langfuse peripheral must be configured for this comparison to "
        "mean anything; reinstall with --components langfuse if it is not."
    )
