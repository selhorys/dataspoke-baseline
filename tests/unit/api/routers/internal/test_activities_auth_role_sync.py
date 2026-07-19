"""Unit tests for POST /internal/activities/auth/role-sync — the reconciliation pass.

This is the branch table of spec/feature/AUTH.md §Role Drift Reconciliation: the
binding gate, the existence probe, the two independent facets, the counter
semantics (explicitly *not* a partition), per-facet failure containment, and the
unconfigured-peripheral no-op.

The endpoint function is invoked directly. Its DataHub and DB dependencies are
acquired inside the function body via ``make_datahub`` / ``make_db_session``
rather than through FastAPI DI, so they are patched at the module level; the
``X-Internal-Token`` gate is covered by tests/unit/api/routers/internal/test_activities.py.

spec: spec/feature/AUTH.md §Role Drift Reconciliation
spec: spec/feature/AUTH.md §Identity-binding requirement
spec: spec/feature/AUTH.md §Failure Modes
spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_GROUP_NAME = "dataspoke-users"
_GROUP_URN = "urn:li:corpGroup:dataspoke-users"


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeSession:
    """Session stub for the pass's single ``select(User)`` query.

    The pass issues exactly one query, so no query routing is needed. Rows added
    via ``add_all`` are captured for the event-persistence assertions.
    """

    def __init__(self, users: list) -> None:
        self._users = users
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._users
        return result

    def add_all(self, rows) -> None:
        self.added.extend(rows)

    async def commit(self) -> None:
        self.commits += 1


def _user(*, email: str, role: str = "Reader", google_sub: str | None = "sub") -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.email = email
    row.role = role
    row.google_sub = google_sub
    return row


@asynccontextmanager
async def _session_ctx(session):
    yield session


def _run_pass(
    session: _FakeSession,
    *,
    dh_overrides: dict,
    datahub_factory=None,
):
    """Invoke ``auth_role_sync`` with the DataHub primitives replaced.

    *dh_overrides* maps ``src.backend.datahub.users`` attribute names to the
    AsyncMock that should replace them; every primitive the pass may call must be
    supplied so a real call can never escape into the SDK.
    """
    from src.api.routers.internal.activities import auth_role_sync

    runtime_config = MagicMock()
    runtime_config.auth_datahub_corp_group = _GROUP_NAME

    if datahub_factory is None:
        datahub_factory = AsyncMock(return_value=MagicMock(name="datahub"))

    patches = [
        patch(
            "src.api.routers.internal.activities.make_db_session",
            lambda: _session_ctx(session),
        ),
        patch("src.api.routers.internal.activities.make_datahub", datahub_factory),
        patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=runtime_config),
        ),
    ]
    for name, mock in dh_overrides.items():
        patches.append(patch(f"src.backend.datahub.users.{name}", new=mock))

    async def _call():
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return await auth_role_sync()

    return _call()


def _dh_mocks(**overrides):
    """Default DataHub primitive mocks: nothing exists, nothing drifts."""
    mocks = {
        "ensure_marker_group_exists": AsyncMock(),
        "corpuser_exists": AsyncMock(return_value=True),
        "read_role": AsyncMock(return_value="Reader"),
        "propagate_role": AsyncMock(),
        "read_native_group_membership": AsyncMock(return_value=[_GROUP_URN]),
        "add_user_to_marker_group": AsyncMock(),
    }
    mocks.update(overrides)
    return mocks


# ── Binding gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unbound_row_is_skipped_unbound_with_no_mutation_attempted() -> None:
    """A row with google_sub IS NULL is counted skipped_unbound and never probed.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 1 — "Binding gate —
    skip rows with `google_sub IS NULL`, counting them `skipped_unbound`."
    spec: spec/feature/AUTH.md §Identity-binding requirement — "A row created by
    password registration alone is never projected, on either path."
    """
    session = _FakeSession([_user(email="pw-only@example.com", google_sub=None)])
    mocks = _dh_mocks()

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 1, "The row is examined, so it counts in checked"
    assert result["skipped_unbound"] == 1, (
        "A google_sub IS NULL row must be counted skipped_unbound per "
        "spec/feature/AUTH.md §Role Drift Reconciliation step 1"
    )
    assert result["skipped_unprovisioned"] == 0, (
        "skipped_unbound and skipped_unprovisioned are mutually exclusive per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["fixed"] == 0
    assert result["errors"] == 0
    assert not mocks["corpuser_exists"].called, (
        "Nothing is attempted for an unbound row — not even the existence probe — "
        "per spec/feature/AUTH.md §Identity-binding requirement"
    )
    for name in ("propagate_role", "add_user_to_marker_group"):
        assert not mocks[name].called, (
            f"{name} must not be called for an unbound row per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )
    assert session.added == [], "No repair happened, so no event row may be emitted"


@pytest.mark.asyncio
async def test_bootstrap_admin_style_row_is_skipped_unbound() -> None:
    """The bootstrap admin has no Google identity and is reported skipped_unbound.

    spec: spec/feature/AUTH.md §Built-in Bootstrap Admin — "dataspoke@dataspoke.local
    — a DataSpoke-only address with no Google identity behind it. The row carries no
    google_sub, so the reconciliation pass reports it as skipped_unbound at the
    binding gate".
    """
    session = _FakeSession(
        [_user(email="dataspoke@dataspoke.local", role="Admin", google_sub=None)]
    )
    mocks = _dh_mocks()

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["skipped_unbound"] == 1
    assert not mocks["corpuser_exists"].called


# ── Existence probe ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bound_row_without_corpuser_is_skipped_unprovisioned() -> None:
    """A bound row whose corpuser does not exist is counted skipped_unprovisioned.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "skipped_unprovisioned |
    Bound rows whose corpuser does not exist."
    spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2 — "If it
    does not resolve, count the user skipped_unprovisioned and mutate nothing."
    """
    session = _FakeSession([_user(email="never-logged-in@example.com")])
    mocks = _dh_mocks(corpuser_exists=AsyncMock(return_value=False))

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 1
    assert result["skipped_unprovisioned"] == 1, (
        "A bound row with no corpuser must be counted skipped_unprovisioned per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["skipped_unbound"] == 0
    assert result["fixed"] == 0
    assert result["errors"] == 0
    # Backstop: the probe ran and returned False — the skip is not from the binding gate.
    mocks["corpuser_exists"].assert_awaited_once()
    for name in ("read_role", "propagate_role", "read_native_group_membership",
                 "add_user_to_marker_group"):
        assert not mocks[name].called, (
            f"{name} must not run for an unprovisioned corpuser — probing precedes "
            "any mutation per spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
        )


# ── Role facet ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_role_drift_is_repaired_with_dataspoke_winning() -> None:
    """DataHub-side role divergence is re-asserted from users.role.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 3 — "On divergence
    from `users.role`, re-assert via `batchAssignRole` — **DataSpoke wins**."
    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 5 — the event detail
    records "the observed and authoritative roles".
    """
    from src.shared.events import AUTH_ROLE_SYNC_FIXED

    # Mixed-case on purpose: the URN assertion below also covers the lowercasing
    # rule end-to-end through the pass.
    row = _user(email="Drifted@Example.com", role="Admin")
    session = _FakeSession([row])
    mocks = _dh_mocks(read_role=AsyncMock(return_value="Editor"))

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["fixed"] == 1, (
        "A repaired role facet counts the user in fixed per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["errors"] == 0
    assert mocks["propagate_role"].await_count == 1
    assert mocks["propagate_role"].await_args.args[1:] == (
        "urn:li:corpuser:drifted@example.com",
        "Admin",
    ), (
        "The projection must address the corpuser derived from this row's own "
        "email, lowercased, and re-assert the DataSpoke role — DataSpoke wins per "
        "spec/feature/AUTH.md §Role Drift Reconciliation step 3, URN per "
        "spec/feature/AUTH.md §URN conventions"
    )
    assert len(session.added) == 1, (
        "One AUTH.ROLE_SYNC_FIXED event is emitted per repaired user per "
        "spec/feature/AUTH.md §Role Drift Reconciliation step 5"
    )
    event = session.added[0]
    assert event.event_type == AUTH_ROLE_SYNC_FIXED
    assert event.entity_id == str(row.id)
    assert event.detail["repaired_facets"] == ["role"], (
        "The event detail names which facet(s) were repaired per "
        "spec/feature/AUTH.md §Role Drift Reconciliation step 5"
    )
    assert event.detail["dataspoke_role_authoritative"] == "Admin"
    assert event.detail["datahub_role_observed"] == "Editor"


@pytest.mark.asyncio
async def test_no_drift_repairs_nothing() -> None:
    """When both facets already match, nothing is written and fixed stays 0.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — repair is conditional on
    divergence (step 3 "On divergence") and on absence (step 4 "If the marker group
    URN is absent").
    """
    session = _FakeSession([_user(email="in-sync@example.com", role="Reader")])
    mocks = _dh_mocks()

    result = await _run_pass(session, dh_overrides=mocks)

    assert result == {
        "checked": 1,
        "fixed": 0,
        "skipped_unprovisioned": 0,
        "skipped_unbound": 0,
        "errors": 0,
    }
    # Backstop: both facets were actually read, so the zero counts mean "no drift",
    # not "never looked".
    mocks["read_role"].assert_awaited_once()
    mocks["read_native_group_membership"].assert_awaited_once()
    assert not mocks["propagate_role"].called
    assert not mocks["add_user_to_marker_group"].called
    assert session.added == []


# ── Group facet ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_marker_group_membership_is_repaired() -> None:
    """A corpuser lacking the marker group URN is added to it.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 4 — "Group facet —
    read the corpuser's `nativeGroupMembership` aspect. If the marker group URN is
    absent, add it via `addGroupMembers`."
    """
    session = _FakeSession([_user(email="Ungrouped@Example.com", role="Reader")])
    mocks = _dh_mocks(read_native_group_membership=AsyncMock(return_value=[]))

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["fixed"] == 1
    assert result["errors"] == 0
    assert mocks["add_user_to_marker_group"].await_count == 1
    assert mocks["add_user_to_marker_group"].await_args.args[1:] == (
        _GROUP_URN,
        "urn:li:corpuser:ungrouped@example.com",
    ), (
        "Membership is added to the marker corpGroup named by "
        "/admin/conf.auth_datahub_corp_group, for the corpuser derived from this "
        "row's own email, lowercased — spec/feature/AUTH.md §Marker corpGroup and "
        "§URN conventions"
    )
    assert not mocks["propagate_role"].called, "The role facet was in sync"
    assert len(session.added) == 1
    assert session.added[0].detail["repaired_facets"] == ["group"]
    assert session.added[0].detail["marker_group_urn"] == _GROUP_URN


@pytest.mark.asyncio
async def test_marker_group_is_asserted_once_before_the_loop() -> None:
    """The marker corpGroup is asserted exactly once per pass, not per user.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "It asserts the marker
    corpGroup **once, unconditionally, before the loop**".
    spec: spec/feature/AUTH.md §Marker corpGroup — "every reconciliation pass asserts
    the group once, unconditionally, before its per-user loop".
    """
    session = _FakeSession(
        [
            _user(email="a@example.com", google_sub="sub-a"),
            _user(email="b@example.com", google_sub="sub-b"),
            _user(email="c@example.com", google_sub=None),
        ]
    )
    # Membership is absent so the group facet actually repairs — without a real
    # addGroupMembers call the ordering below could not be observed at all.
    mocks = _dh_mocks(read_native_group_membership=AsyncMock(return_value=[]))

    # Attach both to a shared parent so their relative order is recorded.
    parent = MagicMock()
    parent.attach_mock(mocks["ensure_marker_group_exists"], "ensure_group")
    parent.attach_mock(mocks["add_user_to_marker_group"], "add_member")

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 3, "Backstop: the loop really iterated three rows"
    assert mocks["ensure_marker_group_exists"].await_count == 1, (
        "The marker group is asserted once per pass regardless of user count per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert mocks["ensure_marker_group_exists"].await_args.args[1] == _GROUP_NAME

    called = [name for name, _, _ in parent.mock_calls]
    assert mocks["add_user_to_marker_group"].await_count == 2, (
        "Backstop: the two bound rows really needed a membership repair"
    )
    assert called.index("ensure_group") < called.index("add_member"), (
        "The group assert must PRECEDE every addGroupMembers call — the mutation "
        "rejects an unresolvable group URN, per spec/feature/AUTH.md "
        "§Role Drift Reconciliation"
    )


# ── Both facets ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_facets_repaired_increments_fixed_once() -> None:
    """A user with both facets repaired counts once in fixed, with both named in detail.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "fixed | Users where at
    least one facet was repaired. The per-facet breakdown lives in the event detail."
    All counters "are per *user*, not per facet".
    """
    session = _FakeSession([_user(email="both@example.com", role="Editor")])
    mocks = _dh_mocks(
        read_role=AsyncMock(return_value="Reader"),
        read_native_group_membership=AsyncMock(return_value=[]),
    )

    result = await _run_pass(session, dh_overrides=mocks)

    # Backstop: both repairs really happened.
    mocks["propagate_role"].assert_awaited_once()
    mocks["add_user_to_marker_group"].assert_awaited_once()
    assert result["fixed"] == 1, (
        "fixed counts users, not facets — two repairs on one user increment it once "
        "per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert len(session.added) == 1, "One event per repaired user, not one per facet"
    assert set(session.added[0].detail["repaired_facets"]) == {"role", "group"}, (
        "The event detail names every repaired facet per "
        "spec/feature/AUTH.md §Role Drift Reconciliation step 5 — the spec "
        "constrains which facets are named, not their order"
    )


# ── Counter overlap: fixed ∩ errors ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_failure_after_role_repair_counts_in_both_fixed_and_errors() -> None:
    """One facet repaired + the other failing puts the user in fixed AND errors.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "The two facets are read
    and repaired independently, so a user can have one facet repaired and the other
    fail. Such a user counts in **both** `fixed` and `errors`. The buckets are
    therefore not a partition and need not sum to `checked`."
    """
    from src.shared.exceptions import DataHubUnavailableError

    session = _FakeSession([_user(email="half-repaired@example.com", role="Admin")])
    mocks = _dh_mocks(
        read_role=AsyncMock(return_value="Reader"),
        read_native_group_membership=AsyncMock(
            side_effect=DataHubUnavailableError("group read failed")
        ),
    )

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 1
    assert result["fixed"] == 1, (
        "The repaired role facet counts the user in fixed per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["errors"] == 1, (
        "The failed group facet counts the same user in errors — the buckets are "
        "not a partition per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    # Backstop: the role repair really landed on DataHub.
    mocks["propagate_role"].assert_awaited_once()
    assert len(session.added) == 1, (
        "The audit row for the repair that already landed in DataHub must persist — "
        "a retry sees no drift and would emit nothing"
    )
    assert session.added[0].detail["repaired_facets"] == ["role"]
    assert session.commits >= 1, "The event rows must be committed"


@pytest.mark.asyncio
async def test_role_facet_failure_does_not_suppress_the_group_repair() -> None:
    """The two facets are attempted independently — a role failure still allows a group fix.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "The two facets are read
    and repaired independently".
    """
    from src.shared.exceptions import DataHubUnavailableError

    session = _FakeSession([_user(email="role-failed@example.com", role="Editor")])
    mocks = _dh_mocks(
        read_role=AsyncMock(side_effect=DataHubUnavailableError("role read failed")),
        read_native_group_membership=AsyncMock(return_value=[]),
    )

    result = await _run_pass(session, dh_overrides=mocks)

    mocks["add_user_to_marker_group"].assert_awaited_once()
    assert result["fixed"] == 1
    assert result["errors"] == 1
    assert session.added[0].detail["repaired_facets"] == ["group"]


@pytest.mark.asyncio
async def test_both_facets_failing_counts_the_user_in_errors_once() -> None:
    """Two failed facets on one user increment errors once, not twice.

    The counter guard: every bucket is per user. A pass that counted per facet
    would report errors == 2 for this single row.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "All counters are per
    *user*, not per facet."
    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "`errors` | Users for
    whom **at least one** facet could not be reconciled."
    """
    from src.shared.exceptions import DataHubUnavailableError

    session = _FakeSession([_user(email="both-failed@example.com", role="Admin")])
    mocks = _dh_mocks(
        read_role=AsyncMock(side_effect=DataHubUnavailableError("role read failed")),
        read_native_group_membership=AsyncMock(
            side_effect=DataHubUnavailableError("group read failed")
        ),
    )

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 1
    assert result["errors"] == 1, (
        "One user with two failed facets counts once — counters are per user, not "
        "per facet, per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["fixed"] == 0, "Nothing was repaired"
    assert session.added == [], "No AUTH.ROLE_SYNC_FIXED event without a repair"


# ── Step-1 abort ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marker_group_assert_failure_aborts_before_the_loop() -> None:
    """A failed one-time group assert stops the pass instead of degrading it.

    The group must resolve before any addGroupMembers call, so a pass that
    continued would attempt membership writes DataHub rejects and under-report
    drift while reporting success.

    spec: spec/feature/AUTH.md §Failure Modes — "Marker corpGroup assert fails at the
    start of a reconciliation pass | The pass aborts before its per-user loop rather
    than degrading ... Retryable error response; no counter result is returned."
    spec: spec/feature/AUTH.md §Marker corpGroup — "the group must precede
    `addGroupMembers`, which rejects an unresolvable group URN".
    spec: spec/feature/BACKEND.md — activity endpoints map DataSpokeError to 400
    (non-retryable) or 500 (retryable).
    """
    from src.shared.exceptions import DataHubUnavailableError

    session = _FakeSession([_user(email="never-reached@example.com")])
    mocks = _dh_mocks(
        ensure_marker_group_exists=AsyncMock(
            side_effect=DataHubUnavailableError("group assert failed")
        )
    )

    result = await _run_pass(session, dh_overrides=mocks)

    assert not mocks["corpuser_exists"].called, (
        "The pass must not enter the per-user loop when the marker group could not "
        "be asserted per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert not mocks["propagate_role"].called
    assert not mocks["add_user_to_marker_group"].called
    assert not isinstance(result, dict), (
        "An aborted pass returns an error response, not a counter result — "
        "reporting zero counters would read as a clean pass over zero drift"
    )
    assert result.status_code >= 500, (
        "The failure is retryable so Airflow retries the run per "
        "spec/feature/AUTH.md §Failure Modes"
    )


# ── Non-DataSpoke exception containment ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sdk_graph_error_is_contained_per_facet_and_does_not_abort_the_pass() -> None:
    """A GraphError on one user's facet neither aborts the pass nor loses earlier events.

    The SDK raises ``GraphError`` — neither a DataSpokeError nor a transport error —
    for an HTTP 200 body carrying an ``errors`` array.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "errors | Users for whom
    at least one facet could not be reconciled. The next nightly run retries." The
    pass continues over the remaining rows; the counter is per user.
    """
    from datahub.configuration.common import GraphError

    first = _user(email="first@example.com", role="Admin")
    second = _user(email="second@example.com", role="Admin")
    third = _user(email="third@example.com", role="Admin")
    session = _FakeSession([first, second, third])

    async def _read_role(_client, urn):
        if urn == "urn:li:corpuser:second@example.com":
            raise GraphError("GraphQL errors array in a 200 body")
        return "Reader"  # drift for first and third

    mocks = _dh_mocks(read_role=AsyncMock(side_effect=_read_role))

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["checked"] == 3, (
        "The pass must continue past a non-DataSpokeError on one user per "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["fixed"] == 2, "The two undamaged users were still repaired"
    assert result["errors"] == 1, "Only the GraphError user counts in errors"
    persisted = {event.entity_id for event in session.added}
    assert persisted == {str(first.id), str(third.id)}, (
        "Event rows accumulated before and after the failing user must all persist"
    )
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_probe_failure_counts_as_error_not_skipped_unprovisioned() -> None:
    """A failing existence probe is an error, not a silent 'no corpuser' skip.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "errors | Users for whom
    at least one facet could not be reconciled. The next nightly run retries."
    Counting a probe failure as skipped_unprovisioned would report a converged state
    that was never verified.
    """
    from datahub.configuration.common import GraphError

    session = _FakeSession([_user(email="probe-failed@example.com")])
    mocks = _dh_mocks(corpuser_exists=AsyncMock(side_effect=GraphError("probe blew up")))

    result = await _run_pass(session, dh_overrides=mocks)

    assert result["errors"] == 1, (
        "A probe failure counts the user in errors so the next nightly run retries "
        "per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert result["skipped_unprovisioned"] == 0, (
        "skipped_unprovisioned means the corpuser was confirmed absent, not that the "
        "probe failed per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert not mocks["propagate_role"].called, (
        "No mutation may follow an unresolved existence probe per "
        "spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
    )


# ── Unconfigured peripheral ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unconfigured_datahub_peripheral_is_a_zero_count_no_op() -> None:
    """With DataHub unconfigured the pass returns zero counts rather than failing.

    spec: spec/feature/AUTH.md §Failure Modes — "DataHub peripheral unconfigured when
    the nightly pass runs | The pass returns a no-op result rather than failing —
    operating before DataHub is wired is a supported steady state."
    """
    from src.shared.exceptions import PeripheralNotConfiguredError

    session = _FakeSession([_user(email="whoever@example.com")])
    mocks = _dh_mocks()

    result = await _run_pass(
        session,
        dh_overrides=mocks,
        datahub_factory=AsyncMock(
            side_effect=PeripheralNotConfiguredError("datahub")
        ),
    )

    assert result == {
        "checked": 0,
        "fixed": 0,
        "skipped_unprovisioned": 0,
        "skipped_unbound": 0,
        "errors": 0,
    }, (
        "An unconfigured DataHub peripheral yields a zero-count no-op, not an error, "
        "per spec/feature/AUTH.md §Failure Modes"
    )
    assert not mocks["ensure_marker_group_exists"].called, (
        "Nothing is asserted against DataHub when the peripheral is unconfigured"
    )
    assert session.added == []


# ── Response shape ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_carries_exactly_the_five_specified_counters() -> None:
    """The pass returns {checked, fixed, skipped_unbound, skipped_unprovisioned, errors}.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "The pass returns
    `{checked, fixed, skipped_unbound, skipped_unprovisioned, errors}`."
    """
    session = _FakeSession([_user(email="shape@example.com")])

    result = await _run_pass(session, dh_overrides=_dh_mocks())

    assert set(result) == {
        "checked",
        "fixed",
        "skipped_unbound",
        "skipped_unprovisioned",
        "errors",
    }, (
        "The response carries exactly the five counters named in "
        "spec/feature/AUTH.md §Role Drift Reconciliation"
    )
