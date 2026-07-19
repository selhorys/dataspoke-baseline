"""Unit tests for GET /api/v1/spoke/common/peripheral-links.

Route under test:
  GET /api/v1/spoke/common/peripheral-links — any authenticated role

Concerns covered:

1. Auth gate: unauthenticated → 401 (not 500, not 200).
2. Any authenticated role reads it — Reader and Editor, not only Admin. This is
   the reason the route exists: /admin/* is Admin-gated and cannot serve the app
   shell to non-Admins.
3. datahub_url is sourced from the DataHub peripheral's ``frontend_url`` and NOT
   from ``gms_url`` — the two are seeded to differ in host, port, AND scheme.
4. The payload carries exactly the three display fields (plus the envelope's
   ``resp_time``): no ``gms_url``, ``kafka_brokers``, or ``service_corpuser_urn``.
5. Unconfigured peripherals yield "" rather than 404.
6. A ``frontend_url`` holding a hostile scheme degrades to "".

Spec traceability:
- spec/API.md §Data Resource — ``GET /spoke/common/peripheral-links``: the payload
  shape, the ``datahub_url`` ⟵ ``datahub.frontend_url`` mapping ("**never**
  ``gms_url``"), the ``langfuse_url`` ⟵ ``langfuse.host`` and
  ``langfuse_project_id`` ⟵ ``langfuse.project_id`` mappings, the ""-means-unset
  rule, the any-authenticated-role gate, and the no-infrastructure-disclosure rule.
- spec/API.md §Access Control — authenticated routes reject a missing bearer token.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.backend.admin.peripheral_service import DatahubConfigDTO, LangfuseConfigDTO
from tests.unit.api.conftest import auth_headers

_PERIPHERAL_LINKS = "/api/v1/spoke/common/peripheral-links"

_PATCH_TARGET = "src.api.routers.spoke.common.peripheral_links.get_peripheral_config"

# The reported deployment's shape: GMS is an internal ELB on plain HTTP port 8080
# while the browser-facing UI is a public TLS hostname. Host, port, and scheme all
# differ, so no heuristic can derive one from the other.
_GMS_URL = "http://datahub-gms.internal:8080"
_FRONTEND_URL = "https://datahub.imazon.example.com"

# Fields the response must never carry — this is a non-Admin surface.
_INFRA_FIELDS = ("gms_url", "kafka_brokers", "service_corpuser_urn", "default_env", "token")


def _peripheral_lookup(
    datahub: DatahubConfigDTO | None, langfuse: LangfuseConfigDTO | None
) -> AsyncMock:
    """Return an async stand-in for ``get_peripheral_config`` keyed by name.

    Routes by the requested peripheral name rather than by call order, so adding
    or reordering a lookup in the router cannot silently shift results.
    """

    async def _lookup(_db: object, name: str) -> DatahubConfigDTO | LangfuseConfigDTO | None:
        if name == "datahub":
            return datahub
        if name == "langfuse":
            return langfuse
        raise AssertionError(f"unexpected peripheral lookup: {name!r}")

    return AsyncMock(side_effect=_lookup)


# ── 1. Auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected_with_401(client) -> None:
    """A call with no bearer token is rejected as unauthenticated.

    Pinned explicitly because the route reads the DB: a guard evaluated after the
    session dependency would surface as a 500 instead of a 401.

    spec: spec/API.md §Data Resource — the route is "Readable by **any
        authenticated role**"; spec/API.md §Access Control.
    """
    resp = await client.get(_PERIPHERAL_LINKS)

    assert resp.status_code == 401, (
        f"Unauthenticated peripheral-links read must be 401, got {resp.status_code}: {resp.text}"
    )
    assert "datahub_url" not in resp.json(), (
        "A rejected request must not leak the peripheral payload"
    )


# ── 2. Any authenticated role ─────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["Reader", "Editor", "Admin"])
@pytest.mark.asyncio
async def test_any_authenticated_role_can_read_the_links(client, role: str) -> None:
    """Reader, Editor, and Admin all read the links.

    The entire reason the route exists: ``/admin/peripherals/datahub`` is
    Admin-gated, so the app shell cannot resolve its links there for a Reader.

    spec: spec/API.md §Data Resource — "Readable by **any authenticated role**
        (the ``/admin/*`` surface is Admin-only, so it cannot serve Readers and
        Editors)".
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from src.api.dependencies import get_db
    from src.api.main import app
    from src.shared.db.models import User

    # The client fixture's user mock is Admin; re-point the user-lookup result at
    # a real User row carrying the role under test (a bare MagicMock cannot pass
    # the isinstance check in the /auth/me backstop below).
    now = datetime.now(tz=UTC)
    session = await anext(app.dependency_overrides[get_db]())
    session.execute.return_value.scalar_one_or_none.return_value = User(
        id=_uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        email="unit-test@example.com",
        name="Unit Test User",
        role=role,
        created_at=now,
        updated_at=now,
    )

    # Backstop: prove the role mutation really reaches the request principal, so
    # a 200 below is the any-role gate rather than an ineffective mutation that
    # silently left every case running as Admin.
    me_resp = await client.get("/api/v1/auth/me", headers=auth_headers())
    assert me_resp.status_code == 200, f"principal lookup failed: {me_resp.text}"
    assert me_resp.json()["role"] == role, (
        f"The request principal resolved as {me_resp.json()['role']!r}, not {role!r} — "
        f"the role mutation did not apply, so this case proves nothing about that role."
    )

    lookup = _peripheral_lookup(
        DatahubConfigDTO(gms_url=_GMS_URL, kafka_brokers="kafka:9092", frontend_url=_FRONTEND_URL),
        LangfuseConfigDTO(
            host="https://langfuse.imazon.example.com",
            public_key="pk",
            project_id="imazon-metadata",
        ),
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, f"{role} must read peripheral-links: {resp.text}"
    assert resp.json()["datahub_url"] == _FRONTEND_URL


# ── 3. datahub_url comes from frontend_url, never gms_url ─────────────────────


@pytest.mark.asyncio
async def test_datahub_url_is_frontend_url_not_gms_url(client) -> None:
    """``datahub_url`` mirrors ``frontend_url``; ``gms_url`` never leaks into it.

    The two seeded values differ in host, port, and scheme, so a mis-wiring that
    reads ``gms_url`` cannot coincidentally produce the expected string.

    spec: spec/API.md §Data Resource — "``datahub_url`` ⟵ ``datahub.frontend_url``
        (the browser-facing UI URL — **never** ``gms_url``, which addresses the
        GMS service and routinely differs in host, port, and scheme)".
    """
    lookup = _peripheral_lookup(
        DatahubConfigDTO(
            gms_url=_GMS_URL,
            kafka_brokers="kafka.internal:9092",
            service_corpuser_urn="urn:li:corpuser:imazon-svc",
            default_env="DEV",
            frontend_url=_FRONTEND_URL,
        ),
        None,
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["datahub_url"] == _FRONTEND_URL, (
        "datahub_url must carry frontend_url verbatim. "
        "spec: spec/API.md §Data Resource — datahub_url ⟵ datahub.frontend_url."
    )
    assert body["datahub_url"] != _GMS_URL, "datahub_url must never be sourced from gms_url"


@pytest.mark.asyncio
async def test_langfuse_fields_come_from_host_and_project_id(client) -> None:
    """``langfuse_url`` mirrors the Langfuse ``host``; ``langfuse_project_id`` its ``project_id``.

    spec: spec/API.md §Data Resource — "``langfuse_url`` ⟵ ``langfuse.host`` (the
        Langfuse peripheral contract names this field ``host``),
        ``langfuse_project_id`` ⟵ ``langfuse.project_id``".
    """
    lookup = _peripheral_lookup(
        None,
        LangfuseConfigDTO(
            host="https://langfuse.imazon.example.com:3443",
            public_key="pk-imazon",
            project_id="imazon-metadata",
            environment_tag="dev",
        ),
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["langfuse_url"] == "https://langfuse.imazon.example.com:3443"
    assert body["langfuse_project_id"] == "imazon-metadata"
    assert "public_key" not in body, "The Langfuse public key is not a display link"
    assert "environment_tag" not in body, "The Langfuse environment tag is not a display link"


# ── 4. Exactly three display fields; no infrastructure disclosure ─────────────


@pytest.mark.asyncio
async def test_payload_carries_only_the_three_display_fields(client) -> None:
    """The response is exactly the three display fields plus the envelope timestamp.

    Every excluded infrastructure field is *seeded* on the DTO first, so the
    absence assertions are non-vacuous: a router that widened the payload would
    emit them.

    spec: spec/API.md §Data Resource — "Returns only these three display fields —
        no ``gms_url``, ``kafka_brokers``, or corpuser URN, so this non-Admin
        surface discloses no infrastructure topology".
    """
    seeded = DatahubConfigDTO(
        gms_url=_GMS_URL,
        kafka_brokers="kafka.internal:9092",
        service_corpuser_urn="urn:li:corpuser:imazon-svc",
        default_env="DEV",
        frontend_url=_FRONTEND_URL,
    )
    lookup = _peripheral_lookup(
        seeded,
        LangfuseConfigDTO(host="https://lf.example.com", public_key="pk", project_id="p1"),
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"datahub_url", "langfuse_url", "langfuse_project_id", "resp_time"}, (
        f"Unexpected peripheral-links payload keys: {sorted(body)}"
    )

    # Backstop for the absence assertions: prove the excluded values were present
    # on the source DTO, so their absence from the payload is a real filter.
    serialized = resp.text
    for field in _INFRA_FIELDS:
        assert field not in body, f"{field} must not appear on this non-Admin surface"
    assert seeded.gms_url and seeded.kafka_brokers and seeded.service_corpuser_urn
    assert seeded.gms_url not in serialized, "The GMS endpoint must not leak in any field"
    assert seeded.kafka_brokers not in serialized, "Kafka brokers must not leak in any field"
    assert seeded.service_corpuser_urn not in serialized, "The corpuser URN must not leak"


# ── 5. Unconfigured peripherals ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unconfigured_peripherals_yield_empty_strings_not_404(client) -> None:
    """With neither peripheral configured the route is 200 with three empty strings.

    spec: spec/API.md §Data Resource — "An unconfigured peripheral yields ``""``,
        which clients read as 'render no link'".
    """
    with patch(_PATCH_TARGET, _peripheral_lookup(None, None)):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, f"Unconfigured peripherals must not 404: {resp.text}"
    body = resp.json()
    assert body["datahub_url"] == ""
    assert body["langfuse_url"] == ""
    assert body["langfuse_project_id"] == ""


@pytest.mark.asyncio
async def test_datahub_row_without_frontend_url_yields_empty_datahub_url(client) -> None:
    """A DataHub peripheral wired for the backend but with no UI URL renders no link.

    This is the state the issue was filed on: GMS fully wired, ``frontend_url``
    never set. The endpoint must report "" rather than inventing a URL from
    ``gms_url``.

    spec: spec/API.md §Data Resource — ``datahub_url`` ⟵ ``datahub.frontend_url``;
        unset yields "".
    """
    lookup = _peripheral_lookup(
        DatahubConfigDTO(gms_url=_GMS_URL, kafka_brokers="kafka:9092"),  # frontend_url defaults ""
        None,
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, resp.text
    assert resp.json()["datahub_url"] == "", (
        "A DataHub peripheral without frontend_url must yield '', "
        "never a value derived from gms_url"
    )


# ── 6. Hostile stored value degrades to "" ────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "stored"),
    [
        ("javascript scheme", "javascript:alert(1)"),
        ("protocol-relative", "//evil.example.com"),
        ("userinfo spoofing", "https://datahub.imazon.example.com@evil.example.com"),
    ],
)
@pytest.mark.asyncio
async def test_hostile_stored_frontend_url_degrades_to_empty(
    client, label: str, stored: str
) -> None:
    """A hostile ``frontend_url`` in the JSONB degrades to "" on the way out.

    ``peripheral_config.settings`` is untyped JSONB, so a row written by direct
    SQL bypasses the admin request schema. The read boundary re-checks.

    spec: spec/API.md §Data Resource → Display-link safety — "On read,
        ``GET /spoke/common/peripheral-links`` coerces one to ``""``:
        ``peripheral_config.settings`` is JSONB, so a row written by direct SQL or
        by dev seeding can bypass the request schema. Degrading to ``""`` reuses
        the documented 'render no link' state rather than failing the whole
        response."
    """
    lookup = _peripheral_lookup(
        DatahubConfigDTO(gms_url=_GMS_URL, kafka_brokers="kafka:9092", frontend_url=stored),
        None,
    )
    with patch(_PATCH_TARGET, lookup):
        resp = await client.get(_PERIPHERAL_LINKS, headers=auth_headers())

    assert resp.status_code == 200, f"{label}: hostile value must degrade, not 500: {resp.text}"
    assert resp.json()["datahub_url"] == "", (
        f"{label}: a stored value that is not a safe http(s) URL must not reach a browser href"
    )
