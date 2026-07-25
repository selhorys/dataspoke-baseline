"""Spot integration test: DELETE /admin/users/{id}/google — the admin unbind.

Concerns covered (one per test):
- a password-less bound row is refused 409 GOOGLE_IS_ONLY_AUTH_METHOD
- a bound row that carries a password unbinds: 204, `has_google` flips to false,
  sessions issued under the released binding stop working, one AUTH.GOOGLE_UNBOUND
  event exists, and the row's API tokens deliberately survive
- an already-unbound row answers 204 without bumping the epoch or writing an event
- a non-Admin caller is refused 403

The rows here are seeded through the ORM/SQL rather than reached through a
pipeline: a bound, password-carrying row is the state a `POST
/auth/password/reset/{request,confirm}` round trip produces, and that round trip
needs a configured SMTP peripheral. Seeding it directly is why this sits at spot
(spec/TESTING.md §Spot integration tests).

spec: spec/feature/AUTH.md §Admin unbind
spec: spec/feature/AUTH.md §Session epoch
spec: spec/API.md §Admin — DELETE /admin/users/{id}/google
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _unique_email(prefix: str = "unbind") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _read_row(session: AsyncSession, user_id: uuid.UUID):
    """Return (google_sub, session_epoch, password_hash) straight from the row."""
    result = await session.execute(
        text(
            "SELECT google_sub, session_epoch, password_hash"
            " FROM dataspoke.users WHERE id = :id"
        ),
        {"id": str(user_id)},
    )
    return result.fetchone()


async def _unbound_events(session: AsyncSession, user_id: uuid.UUID) -> list:
    """Return this user's AUTH.GOOGLE_UNBOUND event rows, bound by entity_id."""
    result = await session.execute(
        text(
            "SELECT entity_type, status, detail FROM dataspoke.events"
            " WHERE event_type = 'AUTH.GOOGLE_UNBOUND' AND entity_id = :id"
        ),
        {"id": str(user_id)},
    )
    return list(result.fetchall())


@pytest.mark.asyncio
async def test_unbind_refuses_a_password_less_bound_row(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    admin_headers: dict[str, str],
) -> None:
    """A bound row with no password_hash is refused 409 GOOGLE_IS_ONLY_AUTH_METHOD.

    This is the normal shape of a bound row — the bind nulls `password_hash` — so
    the refusal is the common path, not an edge case.

    spec: spec/feature/AUTH.md §Admin unbind — "The route refuses with `409
    GOOGLE_IS_ONLY_AUTH_METHOD` when the row has no `password_hash`: clearing
    `google_sub` would violate `ck_users_auth_method` and leave a row nobody can
    authenticate as."
    spec: spec/feature/AUTH.md §Failure Modes — "`DELETE /admin/users/{id}/google`
    on a row with no `password_hash` | Refused before any write".
    """
    user_id = uuid.uuid4()
    google_sub = f"test-sub-{uuid.uuid4()}"
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role, session_epoch)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader', 3)"
        ),
        {
            "id": str(user_id),
            "email": _unique_email("only-google"),
            "name": "Google Only User",
            "google_sub": google_sub,
        },
    )
    await async_session.commit()

    try:
        resp = await api_client.delete(
            f"/api/v1/admin/users/{user_id}/google",
            headers=admin_headers,
        )

        assert resp.status_code == 409, (
            "a password-less bound row must be refused per spec/feature/AUTH.md "
            f"§Admin unbind; got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["error_code"] == "GOOGLE_IS_ONLY_AUTH_METHOD", (
            f"got {resp.json()!r}"
        )

        row = await _read_row(async_session, user_id)
        assert row.google_sub == google_sub, (
            "the refusal happens before any write per spec/feature/AUTH.md §Failure Modes"
        )
        assert row.session_epoch == 3, (
            "a refused unbind changes no credential, so the epoch does not move per "
            "spec/feature/AUTH.md §Session epoch"
        )
        assert await _unbound_events(async_session, user_id) == [], (
            "no binding was released, so no AUTH.GOOGLE_UNBOUND event is written per "
            "spec/feature/AUTH.md §Admin unbind"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(user_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_unbind_releases_the_binding_and_ends_its_sessions(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    admin_headers: dict[str, str],
) -> None:
    """A bound row carrying a password unbinds: 204, has_google false, sessions dead, one event.

    spec: spec/feature/AUTH.md §Admin unbind — "It clears `google_sub` and
    increments `session_epoch` — unbinding is a credential change, so sessions
    established under the released binding do not survive it — and emits one
    `AUTH.GOOGLE_UNBOUND` event (`entity_type = user`, `entity_id` = the user id)".
    spec: spec/API.md §Admin — "`204` — releases the row's Google binding: clears
    `google_sub` and increments `session_epoch`, ending sessions established under
    it."
    """
    from src.backend.auth import users as user_service
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("reclaimed")
    # The row's shape after step 1 of the reclamation sequence: bound, and carrying
    # a password set through POST /auth/password/reset/confirm.
    user = await user_service.create_user(
        async_session, email, "Reclaimed Row", password="reclaimed-password-1"
    )
    user_id = user.id
    await async_session.execute(
        text("UPDATE dataspoke.users SET google_sub = :sub WHERE id = :id"),
        {"sub": f"stale-workspace-sub-{uuid.uuid4()}", "id": str(user_id)},
    )
    await async_session.commit()

    epoch_before = (await _read_row(async_session, user_id)).session_epoch
    pre_unbind_token, _ = issue_access_token(user_id, email, session_epoch=epoch_before)

    try:
        # Backstop: the session established under the binding works right now, so a
        # 401 after the unbind is the epoch bump rather than a token that never worked.
        before = await api_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {pre_unbind_token}"}
        )
        assert before.status_code == 200, (
            f"the pre-unbind session must work before the unbind: {before.text}"
        )
        assert before.json()["has_google"] is True, (
            "the row must actually be bound before the unbind, or this test proves "
            f"nothing; got {before.json()!r}"
        )

        resp = await api_client.delete(
            f"/api/v1/admin/users/{user_id}/google",
            headers=admin_headers,
        )
        assert resp.status_code == 204, (
            "releasing a binding on a password-carrying row must return 204 per "
            f"spec/API.md §Admin; got {resp.status_code}: {resp.text}"
        )

        row = await _read_row(async_session, user_id)
        assert row.google_sub is None, (
            "the unbind clears google_sub per spec/feature/AUTH.md §Admin unbind"
        )
        assert row.session_epoch == epoch_before + 1, (
            "unbinding is a credential change, so it increments session_epoch per "
            f"spec/feature/AUTH.md §Admin unbind; {epoch_before} → {row.session_epoch}"
        )
        assert row.password_hash is not None, (
            "the unbind leaves the row's remaining authentication method in place per "
            "spec/feature/AUTH.md §Admin unbind"
        )

        after = await api_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {pre_unbind_token}"}
        )
        assert after.status_code == 401, (
            "sessions established under the released binding do not survive it per "
            f"spec/feature/AUTH.md §Admin unbind; got {after.status_code}: {after.text}"
        )

        # The row itself, read through a session issued under the new epoch.
        post_unbind_token, _ = issue_access_token(
            user_id, email, session_epoch=row.session_epoch
        )
        me = await api_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {post_unbind_token}"}
        )
        assert me.status_code == 200, f"the post-unbind session must work: {me.text}"
        assert me.json()["has_google"] is False, (
            "the row resolves as unbound after the release per spec/feature/AUTH.md "
            f"§Admin unbind; got {me.json()!r}"
        )

        events = await _unbound_events(async_session, user_id)
        assert len(events) == 1, (
            "a release emits one AUTH.GOOGLE_UNBOUND event per spec/feature/AUTH.md "
            f"§Admin unbind; got {len(events)}"
        )
        assert events[0].entity_type == "user", (
            "the event carries `entity_type = user` per spec/feature/AUTH.md §Admin "
            f"unbind; got {events[0].entity_type!r}"
        )
        # BACKEND_SCHEMA.md §events permits success/ok/failure/error/running/
        # warning/info; `success` is the project's chosen value for a completed
        # AUTH.* write rather than one the spec singles out.
        assert events[0].status == "success", f"got {events[0].status!r}"
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(user_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_unbind_leaves_the_rows_api_tokens_working(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    admin_headers: dict[str, str],
) -> None:
    """A PAT minted before the unbind keeps working — the asymmetry is deliberate.

    spec: spec/feature/AUTH.md §Admin unbind — "It does **not** revoke the row's
    API tokens, and the PAT authentication path runs no epoch check, so tokens
    minted before the unbind keep working."
    """
    from src.backend.auth import users as user_service
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("pat-survives")
    user = await user_service.create_user(
        async_session, email, "PAT Survivor", password="pat-survivor-password-1"
    )
    user_id = user.id
    await async_session.execute(
        text("UPDATE dataspoke.users SET google_sub = :sub WHERE id = :id"),
        {"sub": f"stale-workspace-sub-{uuid.uuid4()}", "id": str(user_id)},
    )
    await async_session.commit()

    epoch_before = (await _read_row(async_session, user_id)).session_epoch
    jwt_token, _ = issue_access_token(user_id, email, session_epoch=epoch_before)

    try:
        mint = await api_client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "survives-the-unbind"},
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        assert mint.status_code == 201, f"pre-unbind mint must succeed: {mint.text}"
        api_token = mint.json()["token"]

        resp = await api_client.delete(
            f"/api/v1/admin/users/{user_id}/google", headers=admin_headers
        )
        assert resp.status_code == 204, f"the unbind must succeed: {resp.text}"

        # Backstop: the unbind really moved the epoch, so the PAT's survival below
        # is the PAT path skipping the check rather than nothing having happened.
        row = await _read_row(async_session, user_id)
        assert row.session_epoch == epoch_before + 1

        me = await api_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {api_token}"}
        )
        assert me.status_code == 200, (
            "an unbind must not revoke the row's API tokens, and the PAT path runs no "
            f"epoch check, per spec/feature/AUTH.md §Admin unbind; got {me.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(user_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_unbind_on_an_already_unbound_row_is_an_idempotent_noop(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    admin_headers: dict[str, str],
) -> None:
    """An already-unbound row answers 204 with no epoch bump and no event.

    spec: spec/feature/AUTH.md §Admin unbind — "The route is idempotent: an
    already-unbound row is left untouched and still answers `204`. There is no
    binding to release, so there is no credential change, and bumping the epoch
    there would sign the user out of every session for nothing".
    """
    from src.backend.auth import users as user_service
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("never-bound")
    user = await user_service.create_user(
        async_session, email, "Password Only User", password="never-bound-password-1"
    )
    user_id = user.id
    await async_session.commit()

    epoch_before = (await _read_row(async_session, user_id)).session_epoch
    session_token, _ = issue_access_token(user_id, email, session_epoch=epoch_before)

    try:
        resp = await api_client.delete(
            f"/api/v1/admin/users/{user_id}/google", headers=admin_headers
        )
        assert resp.status_code == 204, (
            "an already-unbound row still answers 204 per spec/feature/AUTH.md "
            f"§Admin unbind; got {resp.status_code}: {resp.text}"
        )

        row = await _read_row(async_session, user_id)
        assert row.session_epoch == epoch_before, (
            "no binding was released, so the epoch must not move per "
            f"spec/feature/AUTH.md §Admin unbind; {epoch_before} → {row.session_epoch}"
        )
        assert await _unbound_events(async_session, user_id) == [], (
            "an untouched row writes no AUTH.GOOGLE_UNBOUND event per "
            "spec/feature/AUTH.md §Admin unbind"
        )

        # The user-visible consequence of not bumping: their session survives.
        me = await api_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"}
        )
        assert me.status_code == 200, (
            "bumping the epoch there would sign the user out for nothing per "
            f"spec/feature/AUTH.md §Admin unbind; got {me.status_code}: {me.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(user_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_unbind_is_refused_for_a_non_admin_caller(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """A Reader calling the unbind route gets 403 and the row keeps its binding.

    spec: spec/feature/AUTH.md §Privilege Model — "`/admin/*` | ✗ | ✗ | ✓ all
    methods"; "Editor or Reader attempting `/admin/*` | `403 FORBIDDEN`".
    spec: spec/feature/AUTH.md §Admin Surface — "`/admin/*` routes require
    `users.role = 'Admin'`".
    """
    from src.backend.auth.tokens import issue_access_token

    target_id = uuid.uuid4()
    target_sub = f"test-sub-{uuid.uuid4()}"
    reader_id = uuid.uuid4()
    reader_email = _unique_email("reader-caller")

    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(target_id),
            "email": _unique_email("unbind-target"),
            "name": "Unbind Target",
            "google_sub": target_sub,
        },
    )
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Reader Caller",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()

    reader_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)

    try:
        resp = await api_client.delete(
            f"/api/v1/admin/users/{target_id}/google",
            headers={"Authorization": f"Bearer {reader_token}"},
        )

        assert resp.status_code == 403, (
            "a non-Admin caller must be refused on /admin/* per spec/feature/AUTH.md "
            f"§Privilege Model; got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["error_code"] == "FORBIDDEN", f"got {resp.json()!r}"

        row = await _read_row(async_session, target_id)
        assert row.google_sub == target_sub, (
            "a refused call writes nothing — the binding must survive the 403"
        )
    finally:
        for row_id in (target_id, reader_id):
            await async_session.execute(
                text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(row_id)}
            )
        await async_session.commit()
