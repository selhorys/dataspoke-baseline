"""Spot tests for the sync sweep's dataset-attribute mirror.

The `datahub-sync-hourly` sweep refreshes the `dataset_registry` columns every
`dataset_filter` is evaluated against. These assertions read the registry columns
directly through asyncpg, which is what makes spot the right layer: no public route
returns `origin` / `platform_urn` / `tag_urns` / `glossary_term_urns` / `is_primary`
row by row, and `GET /spoke/governance/metric/{id}/dataset` exposes only the aggregated
`attrs_synced_at` watermark.

Concerns covered (one per test):
- the sweep populates the attribute columns for the seeded catalog datasets
- `origin` and `platform_urn` are the URN's own segments, not a fetched field
- the sweep reports its coverage under the `attrs_synced` summary counter
- a second sweep refreshes rather than blanks — the never-blank upsert rule
- a filter written against the synced attributes resolves to the datasets that carry them
- a boolean `is_primary` clause selects each side of the column, with the non-primary
  side seeded directly in the registry (the dev DataHub estate carries no `siblings`
  aspect, so the `false` side is not otherwise reachable from here)
- a never-swept row is absent from **both** directions of a scalar clause (`NULL`
  matches neither) while matching every array `NOT IN` (the empty array contains
  nothing) — the never-swept asymmetry, seeded directly because a row the sweep has
  reached is by definition no longer never-swept

Spec:
- spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — the two-source split, the
  hardening properties, and the never-blank upsert rule
- spec/feature/BACKEND_SCHEMA.md §dataset_registry — the attribute columns
- spec/feature/BACKEND.md §Sync + mapping sweep — step 3 and the `attrs_synced` counter
- spec/API.md §`dataset_filter` grammar — the columns a filter reads
"""

import os
from contextlib import suppress

import asyncpg
import httpx
import pytest

# Per-module dummy-data seed: the catalog tables must exist in DataHub for the sweep
# to enumerate and read them.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_TITLE_MASTER_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_EDITIONS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)

# Registry rows written directly for the never-swept negation test below. They are
# never emitted to DataHub: the state under test is a row the attribute sweep has not
# reached, which no DataHub-backed setup can produce.
_NEVER_SWEPT_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.spot_never_swept,EI)"
)
_SWEPT_TAGGED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.spot_swept_tagged,EI)"
)
_SWEPT_OTHER_ORIGIN_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.spot_swept_other,STG)"
)
_NEGATION_TAG_URN = "urn:li:tag:spot-negation"


async def _get_ds_conn() -> asyncpg.Connection:
    """Open a direct asyncpg connection to the DataSpoke operational DB."""
    return await asyncpg.connect(
        host=os.environ.get("DATASPOKE_DEV_POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("DATASPOKE_DEV_POSTGRES_PORT", "9201")),
        user=os.environ.get("DATASPOKE_DEV_POSTGRES_USER", "dataspoke"),
        password=os.environ.get("DATASPOKE_DEV_POSTGRES_PASSWORD", ""),
        database=os.environ.get("DATASPOKE_DEV_POSTGRES_DB", "dataspoke"),
    )


async def _registry_row(conn: asyncpg.Connection, dataset_urn: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT dataset_urn, datahub_registered, origin, platform_urn, tag_urns, "
        "glossary_term_urns, is_primary, attrs_synced_at "
        "FROM dataspoke.dataset_registry WHERE dataset_urn = $1",
        dataset_urn,
    )
    return dict(row) if row is not None else None


async def _run_sweep(api_client: httpx.AsyncClient, internal_headers: dict[str, str]) -> dict:
    """Trigger the sweep the `datahub-sync-hourly` DAG's single task calls."""
    resp = await api_client.post(
        "/internal/activities/ingestion/sync", headers=internal_headers, timeout=300.0
    )
    assert resp.status_code == 200, (
        f"POST /internal/activities/ingestion/sync expected 200; "
        f"got {resp.status_code}: {resp.text}"
    )
    return resp.json()


@pytest.mark.asyncio
async def test_the_sweep_populates_the_filter_attribute_columns(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """After a sweep, a registered dataset carries every attribute column.

    Spec: spec/feature/BACKEND_SCHEMA.md §dataset_registry — "**Attribute sync**: the
          same sweep refreshes the attribute columns";
    Spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — the column table.
    """
    await _run_sweep(api_client, internal_headers)

    conn = await _get_ds_conn()
    try:
        row = await _registry_row(conn, _TITLE_MASTER_URN)
        assert row is not None, (
            f"{_TITLE_MASTER_URN} must be in dataset_registry after the sweep. "
            "spec: BACKEND_SCHEMA.md §dataset_registry — Creation / reconcile."
        )
        assert row["datahub_registered"] is True
        assert row["attrs_synced_at"] is not None, (
            "attrs_synced_at records when the attribute columns were last refreshed; "
            "null after a sweep means step 3 did not reach this row. "
            "spec: BACKEND_SCHEMA.md §dataset_registry."
        )
        # Arrays are NOT NULL with a '{}' default, so an empty list is a read answer
        # ("no tags"), distinct from NULL, which the column does not admit.
        assert row["tag_urns"] is not None
        assert row["glossary_term_urns"] is not None
        # `is_primary` is NOT NULL for the same reason, and the seeded Imazon estate
        # carries no `siblings` aspect, so "no sibling information ⇒ primary" is the
        # branch a swept seed row must land in.
        # spec: BACKEND_SCHEMA.md §dataset_registry — "`is_primary` | `BOOLEAN` NOT
        #   NULL DEFAULT `true` | `true` when the dataset is the primary member of its
        #   DataHub `siblings` set, or has no siblings";
        # spec: DATAHUB_INTEGRATION.md §Dataset attribute sync — "the aspect absent or
        #   null ⇒ `true`".
        assert row["is_primary"] is True, (
            f"a seeded dataset has no siblings, so the sweep must read it as primary; "
            f"got {row['is_primary']!r}. spec: DATAHUB_INTEGRATION.md §Dataset "
            "attribute sync."
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_origin_and_platform_urn_are_the_urns_own_segments(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """`origin` and `platform_urn` mirror the URN, not a fetched GraphQL field.

    Spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "`origin`,
          `platform_urn` | parsed from the dataset URN |
          `urn:li:dataset:(<platform_urn>,<name>,<origin>)` encodes both by definition";
    Spec: spec/API.md §`dataset_filter` grammar — "`origin` […] The URN's third
          segment"; "`platform_urn` […] The URN's first segment".
    """
    await _run_sweep(api_client, internal_headers)

    conn = await _get_ds_conn()
    try:
        for urn in (_TITLE_MASTER_URN, _EDITIONS_URN):
            row = await _registry_row(conn, urn)
            assert row is not None, f"{urn} must be registered after the sweep"
            assert row["origin"] == "DEV", (
                f"{urn}: origin must be the URN's third segment; got {row['origin']!r}. "
                "spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
            )
            assert row["platform_urn"] == "urn:li:dataPlatform:postgres", (
                f"{urn}: platform_urn must be the URN's first segment; got "
                f"{row['platform_urn']!r}. "
                "spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_sweep_summary_reports_attribute_coverage(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """`attrs_synced` counts rows refreshed, so it stays non-zero on a steady estate.

    It is the one summary counter that is not a state-change delta: the question a
    narrowed filter raises is how much of the estate the attribute read covered.

    Spec: spec/feature/BACKEND.md §Sync + mapping sweep — Sweep summary — "attrs_synced
          — dataset_registry rows whose filter attributes were refreshed by step 3. The
          exception to the state-change rule: it counts rows refreshed, not rows changed
          […] Zero means the read or the write degraded".
    """
    first = await _run_sweep(api_client, internal_headers)
    assert "attrs_synced" in first, (
        f"the sweep summary must report attrs_synced; got keys {sorted(first)}. "
        "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
    )
    assert first["attrs_synced"] > 0, (
        f"the seeded catalog datasets must be covered by the attribute read; got "
        f"{first['attrs_synced']}. spec: feature/BACKEND.md §Sync + mapping sweep."
    )

    second = await _run_sweep(api_client, internal_headers)
    assert second["attrs_synced"] > 0, (
        "attrs_synced reports coverage, not change, so a second consecutive sweep over "
        f"an unchanged estate still reports it; got {second['attrs_synced']}. "
        "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
    )


@pytest.mark.asyncio
async def test_a_second_sweep_refreshes_rather_than_blanks(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """Re-running the sweep advances `attrs_synced_at` and keeps the attributes.

    The upsert rule is load-bearing: a sweep that blanked `tag_urns` would silently
    narrow every UC3/UC4/UC5 filter instead of failing visibly.

    Spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "The sweep **upserts per
          dataset and never deletes-then-inserts** […] a half-completed read that emptied
          `tag_urns` would silently narrow every filter in the system".
    """
    await _run_sweep(api_client, internal_headers)

    conn = await _get_ds_conn()
    try:
        before = await _registry_row(conn, _TITLE_MASTER_URN)
        assert before is not None and before["attrs_synced_at"] is not None, (
            "backstop: the first sweep must have synced this row"
        )
    finally:
        await conn.close()

    await _run_sweep(api_client, internal_headers)

    conn = await _get_ds_conn()
    try:
        after = await _registry_row(conn, _TITLE_MASTER_URN)
        assert after is not None
        assert after["attrs_synced_at"] > before["attrs_synced_at"], (
            "each sweep stamps the row with its own run time, so the watermark must "
            "strictly advance; a watermark that merely held would mean the second "
            "sweep wrote nothing — the frozen-scope failure the refresh exists to "
            "prevent. spec: BACKEND_SCHEMA.md §dataset_registry — attrs_synced_at is "
            "'When the attribute columns above were last refreshed'."
        )
        assert after["origin"] == before["origin"]
        assert after["platform_urn"] == before["platform_urn"]
        assert after["is_primary"] == before["is_primary"], (
            "a re-sweep must not flip a dataset's sibling verdict. "
            "spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
        )
        assert list(after["tag_urns"]) == list(before["tag_urns"]), (
            "a re-sweep must not blank the association columns. "
            "spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
        )
        assert after["datahub_registered"] is True, (
            "the attribute step must not deregister a dataset reconcile registered. "
            "spec: BACKEND_SCHEMA.md §dataset_registry — Creation / reconcile."
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_filter_resolves_against_the_synced_attributes(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """An `origin`-scoped filter covers exactly the datasets whose synced origin matches.

    This is the end of the chain the sweep exists to serve: the columns it mirrors are
    what `dataset_filter` is evaluated against.

    Spec: spec/API.md §`dataset_filter` grammar — "`dataset_filter` is a SQL
          `WHERE`-clause string evaluated against `dataset_registry`, DataSpoke's local
          mirror of the DataHub dataset estate".
    """
    await _run_sweep(api_client, internal_headers)

    _METRIC_ID = "spot-attr-sync-filter"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Attribute sync filter spot test",
                "description": "A filter reads the columns the sweep mirrored.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": "origin = 'DEV'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        expected = await conn.fetchval(
            "SELECT count(*) FROM dataspoke.dataset_registry "
            "WHERE datahub_registered = TRUE AND origin = 'DEV'"
        )
        assert expected > 0, (
            "the sweep must have written origin='DEV' for the seeded datasets, or this "
            "assertion is vacuous. spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
        )

        view = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
        assert view.status_code == 200, view.text
        assert view.json()["total_count"] == expected, (
            f"the filter must cover exactly the {expected} DEV-origin registered "
            f"datasets; got {view.json()['total_count']}. "
            "spec: API.md §`dataset_filter` grammar."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        await conn.close()


@pytest.mark.asyncio
async def test_a_boolean_filter_resolves_against_the_synced_is_primary(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """`is_primary = false` selects the non-primary rows and `= true` the complement.

    Both sides are seeded: the dev DataHub estate carries no `siblings` aspect, so
    every swept row reads `true` and a `false` clause would match nothing — an
    over-broad predicate would then be indistinguishable from a correct one. The
    non-primary side is written straight into `dataset_registry` here because that is
    the state a `dataset_filter` is evaluated against; the DataHub-side derivation
    that produces it is covered by the UC5 api-wired arc, which emits a real
    `siblings` aspect.

    Spec: spec/API.md §`dataset_filter` grammar — "`is_primary` | bool | `true` when
          the dataset is the primary member of its DataHub sibling set, or has no
          siblings. `is_primary = true` scopes a filter to one row per logical asset";
    Spec: spec/feature/BACKEND_SCHEMA.md §`dataset_registry` — the `is_primary` column.
    """
    await _run_sweep(api_client, internal_headers)

    _METRIC_ID = "spot-attr-sync-is-primary"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        snapshot = await conn.fetchval(
            "SELECT is_primary FROM dataspoke.dataset_registry WHERE dataset_urn = $1",
            _EDITIONS_URN,
        )
        assert snapshot is True, (
            f"backstop: {_EDITIONS_URN} must be a swept, primary row before this test "
            f"demotes it; got {snapshot!r}. spec: DATAHUB_INTEGRATION.md §Dataset "
            "attribute sync."
        )

        try:
            # Demote one seeded dataset to a non-leading sibling — the `false` side of
            # the predicate under test.
            await conn.execute(
                "UPDATE dataspoke.dataset_registry SET is_primary = FALSE "
                "WHERE dataset_urn = $1",
                _EDITIONS_URN,
            )

            create_resp = await api_client.post(
                "/api/v1/spoke/governance/metric",
                headers=admin_headers,
                json={
                    "metric_id": _METRIC_ID,
                    "mode": "active",
                    "is_enabled": False,
                    "metric_type": "doc-health",
                    "title": "Sibling-scoped documentation health",
                    "description": "A boolean clause reads the column the sweep mirrored.",
                    "metrics": [
                        {"name": "total", "color": "#64748B", "idx": 1},
                        {"name": "doc_health", "color": "#A855F7", "idx": 2},
                    ],
                    "metric_conf": {},
                    "schedule_tier": None,
                    "dataset_filter": "is_primary = false",
                },
            )
            assert create_resp.status_code == 201, create_resp.text

            false_view = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
            assert false_view.status_code == 200, false_view.text
            false_urns = {row["dataset_urn"] for row in false_view.json()["datasets"]}
            assert false_urns == {_EDITIONS_URN}, (
                f"`is_primary = false` must select exactly the demoted row; got "
                f"{sorted(false_urns)}. spec: API.md §`dataset_filter` grammar."
            )

            # The complement: same estate, opposite clause.
            patch_resp = await api_client.patch(
                base_conf,
                headers=admin_headers,
                json={"dataset_filter": "is_primary = true"},
            )
            assert patch_resp.status_code == 200, patch_resp.text

            true_view = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
            assert true_view.status_code == 200, true_view.text
            true_urns = {row["dataset_urn"] for row in true_view.json()["datasets"]}
            assert _TITLE_MASTER_URN in true_urns, (
                "`is_primary = true` must keep the primary seeded datasets in scope; got "
                f"{sorted(true_urns)}. spec: API.md §`dataset_filter` grammar."
            )
            assert _EDITIONS_URN not in true_urns, (
                "the demoted row must be excluded — otherwise the clause is a no-op and "
                "the sibling double-count it exists to prevent is still there. "
                "spec: API.md §`dataset_filter` grammar."
            )
        finally:
            with suppress(Exception):
                await api_client.delete(base_conf, headers=admin_headers)
            await conn.execute(
                "UPDATE dataspoke.dataset_registry SET is_primary = $2 "
                "WHERE dataset_urn = $1",
                _EDITIONS_URN,
                snapshot,
            )
            restored = await conn.fetchval(
                "SELECT is_primary FROM dataspoke.dataset_registry WHERE dataset_urn = $1",
                _EDITIONS_URN,
            )
            assert restored == snapshot, (
                f"the demoted row must be restored to {snapshot!r} or every later "
                f"`is_primary` assertion in the suite runs against a mutated registry; "
                f"got {restored!r}. spec: TESTING.md §Integration Lifecycle & Isolation."
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_never_swept_row_is_outside_both_scalar_directions_but_inside_every_not_in(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Negation is not a complement of the scope, and the two column kinds differ.

    Three rows are written straight into `dataset_registry`, which is the state a
    `dataset_filter` is evaluated against and the only way to reach the case under test:
    a row the sweep has **not** reached is by construction one no DataHub-backed setup
    can produce, since anything ingested is swept.

      - never-swept: `origin` NULL and `tag_urns` at its `'{}'` default
      - swept + tagged: `origin = 'EI'`, carrying the negation tag
      - swept, other origin: `origin = 'STG'`, no tags — the positive member that keeps
        the `origin != 'EI'` leg from passing on an empty result

    Each of the four clauses is read back through `GET .../dataset`, whose scope is the
    compiled filter pushed into a query over the registry. Both sides of both column
    kinds are asserted, so an over-broad predicate (one folding NULLs in) and an
    over-narrow one (one dropping the empty-array rows) each fail.

    Spec: spec/API.md §`dataset_filter` grammar — Negation: "a row whose scalar column
          is `NULL` satisfies *neither* `scalar_col = 'x'` nor `scalar_col != 'x'` for
          any `x`"; "the array columns are not [nullable], so `NOT IN` over `tag_urns` /
          `glossary_term_urns` *is* a true complement of `IN`";
    Spec: spec/API.md §`dataset_filter` grammar — Never-swept datasets: "`tag_urns` /
          `glossary_term_urns` default to the empty array, which contains nothing, so a
          never-swept dataset matches **every** `NOT IN` predicate over them — there,
          negation widens where the affirmative form narrows"; "`origin` and
          `platform_urn` default to `NULL` until the sweep parses them from the URN, and
          a `NULL` scalar matches neither direction".
    """
    _METRIC_ID = "spot-never-swept-negation"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        # Written, not swept: only the primary key and the registered flag are set, so
        # `origin` stays NULL and `tag_urns` keeps its empty-array default.
        await conn.execute(
            "INSERT INTO dataspoke.dataset_registry (dataset_urn, datahub_registered) "
            "VALUES ($1, TRUE) "
            "ON CONFLICT (dataset_urn) DO UPDATE SET datahub_registered = TRUE, "
            "origin = NULL, platform_urn = NULL, tag_urns = '{}'::text[], "
            "attrs_synced_at = NULL",
            _NEVER_SWEPT_URN,
        )
        for urn, origin, tags in (
            (_SWEPT_TAGGED_URN, "EI", [_NEGATION_TAG_URN]),
            (_SWEPT_OTHER_ORIGIN_URN, "STG", []),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.dataset_registry "
                "(dataset_urn, datahub_registered, origin, platform_urn, tag_urns, "
                " attrs_synced_at) "
                "VALUES ($1, TRUE, $2, $3, $4::text[], now()) "
                "ON CONFLICT (dataset_urn) DO UPDATE SET datahub_registered = TRUE, "
                "origin = $2, platform_urn = $3, tag_urns = $4::text[], "
                "attrs_synced_at = now()",
                urn,
                origin,
                "urn:li:dataPlatform:postgres",
                tags,
            )

        seeded = await conn.fetchrow(
            "SELECT origin, tag_urns, attrs_synced_at FROM dataspoke.dataset_registry "
            "WHERE dataset_urn = $1",
            _NEVER_SWEPT_URN,
        )
        assert seeded is not None and seeded["origin"] is None, (
            "backstop: the row under test must actually carry a NULL origin, or every "
            f"scalar assertion below is about a different state; got {seeded!r}. "
            "spec: BACKEND_SCHEMA.md §dataset_registry."
        )
        assert list(seeded["tag_urns"]) == [] and seeded["attrs_synced_at"] is None, (
            f"backstop: the row must be unswept with an empty tag array; got {seeded!r}."
        )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Never-swept negation spot test",
                "description": "Negation is not a complement of the scope.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": "origin = 'EI'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        async def scope_of(dataset_filter: str) -> set[str]:
            """URNs the metric's scope covers under *dataset_filter*.

            `GET .../dataset` pushes the compiled clause into its own paginated query
            over `dataset_registry`, so the page is the filter's scope — no run needed.
            spec: feature/BACKEND.md §Metrics Service — "it pushes the compiled filter
            clause into a paginated query over `dataset_registry`".
            """
            patch_resp = await api_client.patch(
                base_conf, headers=admin_headers, json={"dataset_filter": dataset_filter}
            )
            assert patch_resp.status_code == 200, patch_resp.text
            view = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
            assert view.status_code == 200, view.text
            return {row["dataset_urn"] for row in view.json()["datasets"]}

        # ── Scalar, affirmative ───────────────────────────────────────────────
        affirmative = await scope_of("origin = 'EI'")
        assert _SWEPT_TAGGED_URN in affirmative, (
            "the swept EI row must be in the affirmative scope, or the negated leg "
            f"below excludes nothing; got {sorted(affirmative)}."
        )
        assert _NEVER_SWEPT_URN not in affirmative, (
            "a NULL origin does not match `origin = 'EI'`. "
            "spec: API.md §`dataset_filter` grammar — Never-swept datasets."
        )

        # ── Scalar, negated: the never-swept row is in neither direction ──────
        negated = await scope_of("origin != 'EI'")
        assert _SWEPT_OTHER_ORIGIN_URN in negated, (
            "the swept STG row must be in the negated scope, or this leg passes on an "
            f"empty result and proves nothing; got {sorted(negated)}."
        )
        assert _SWEPT_TAGGED_URN not in negated, (
            f"`origin != 'EI'` must exclude the EI row; got {sorted(negated)}."
        )
        assert _NEVER_SWEPT_URN not in negated, (
            "the never-swept row satisfies NEITHER direction of a scalar clause — this "
            "is the whole property: `!=` compiles to plain three-valued SQL, so an "
            "`IS DISTINCT FROM`, a `COALESCE`, or an `OR col IS NULL` would silently "
            f"fold the unswept remainder into every negated scope. Got {sorted(negated)}. "
            "spec: API.md §`dataset_filter` grammar — Negation, Never-swept datasets."
        )

        # ── Array, affirmative ────────────────────────────────────────────────
        tagged = await scope_of(f"'{_NEGATION_TAG_URN}' IN tag_urns")
        assert tagged == {_SWEPT_TAGGED_URN}, (
            "only the row carrying the tag may be in the affirmative array scope; got "
            f"{sorted(tagged)}."
        )

        # ── Array, negated: the never-swept row IS in scope ───────────────────
        untagged = await scope_of(f"'{_NEGATION_TAG_URN}' NOT IN tag_urns")
        assert _NEVER_SWEPT_URN in untagged, (
            "the array columns are NOT NULL with an empty-array default, so a "
            "never-swept row's empty array contains nothing and matches EVERY `NOT IN` "
            "predicate — the opposite of how the same row behaves under a scalar "
            f"negation above; got {sorted(untagged)}. "
            "spec: API.md §`dataset_filter` grammar — Never-swept datasets."
        )
        assert _SWEPT_TAGGED_URN not in untagged, (
            "`NOT IN` over an array column is an exact complement of `IN`, so the tagged "
            f"row must be excluded; got {sorted(untagged)}."
        )

    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.dataset_registry WHERE dataset_urn = ANY($1::text[])",
                [_NEVER_SWEPT_URN, _SWEPT_TAGGED_URN, _SWEPT_OTHER_ORIGIN_URN],
            )
        leftover = await conn.fetchval(
            "SELECT count(*) FROM dataspoke.dataset_registry "
            "WHERE dataset_urn = ANY($1::text[])",
            [_NEVER_SWEPT_URN, _SWEPT_TAGGED_URN, _SWEPT_OTHER_ORIGIN_URN],
        )
        await conn.close()
        assert leftover == 0, (
            f"{leftover} synthetic registry row(s) survived teardown; every later "
            "estate-wide count in the suite would run against a polluted registry. "
            "spec: TESTING.md §Integration Lifecycle & Isolation."
        )
