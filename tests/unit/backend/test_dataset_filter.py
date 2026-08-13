"""Tests for src/backend/_dataset_filter.py — the shared scope resolver.

Resolution is one SQL query over ``dataset_registry``, so what a unit test can hold
the resolver to is the *statement it issues* and the *derivation it performs on the
rows that come back*: the registry restriction, the compiled filter clause, the
explicit-URN narrowing, and how ``unresolved_urns`` is computed. Whether PostgreSQL
then matches the right rows is a property of the compiled clause plus real data and
is covered by ``tests/integration/spot/test_metrics.py`` — the fake session here
returns the rows the query is *told* to return and never re-implements matching,
which would make the assertion circular.

Spec traceability:
- spec/feature/BACKEND.md §Dataset resolution — the two-stage table, "run as one
  query restricted to `datahub_registered = true`", "An empty filter is the bare
  registered set", the bound-parameter invariant, and the no-materialised-list rule
- spec/API.md §`dataset_filter` grammar — `unresolved_urns` semantics
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from src.backend._dataset_filter import (
    dataset_filter_clause,
    resolve_dataset_scope,
    validate_dataset_filter_service,
)
from src.shared.dataset_filter import DatasetFilterSyntaxError
from src.shared.exceptions import InvalidDatasetUrnError

_URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,PROD)"
_URN_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.author_master,PROD)"
_URN_C = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"


class _RegistryQuery:
    """A fake session standing in for ``dataset_registry``.

    It records the statement the resolver issued and hands back the seeded URNs —
    the rows a registry holding exactly those datasets would return. It does not
    evaluate the filter: re-implementing matching in the fake would make every
    assertion about the clause circular.
    """

    def __init__(self, returns: list[str]) -> None:
        self.returns = returns
        self.statements: list[Any] = []

    def wire(self, db: AsyncMock) -> None:
        async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
            self.statements.append(stmt)
            result = MagicMock()
            result.scalars.return_value.all.return_value = list(self.returns)
            return result

        db.execute = AsyncMock(side_effect=_execute)

    @property
    def sql(self) -> str:
        assert len(self.statements) == 1, (
            f"scope resolution is ONE query; got {len(self.statements)}. "
            "spec: feature/BACKEND.md §Dataset resolution."
        )
        return str(self.statements[0].compile(dialect=postgresql.dialect()))

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.statements[0].compile(dialect=postgresql.dialect()).params)


# ── The query the resolver issues ────────────────────────────────────────────


class TestResolutionQuery:
    async def test_scope_is_one_query_over_the_registry(self, db: AsyncMock) -> None:
        """spec: feature/BACKEND.md §Dataset resolution — "A SQLAlchemy boolean
        expression over `dataset_registry`, run as one query"."""
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        await resolve_dataset_scope(db, "origin = 'PROD'")

        assert "dataset_registry" in registry.sql
        assert "dataset_registry.dataset_urn" in registry.sql

    async def test_only_registered_datasets_are_in_scope(self, db: AsyncMock) -> None:
        """spec: feature/BACKEND.md §Dataset resolution — "restricted to
        `datahub_registered = true`"."""
        registry = _RegistryQuery([])
        registry.wire(db)

        await resolve_dataset_scope(db, "origin = 'PROD'")

        # The polarity is the whole restriction: `is_(False)` names the same column
        # and returns the exact complement — every dataset DataHub does not know
        # about — so naming the column is not enough to pin the contract.
        assert "dataset_registry.datahub_registered is true" in registry.sql.lower(), (
            "scope must be restricted to `datahub_registered = true`; "
            f"got:\n{registry.sql}"
        )

    async def test_the_empty_filter_is_the_bare_registered_set(self, db: AsyncMock) -> None:
        """An empty filter adds no predicate beyond the registry restriction.

        spec: feature/BACKEND.md §Dataset resolution — "An empty filter is the bare
            registered set".
        """
        registry = _RegistryQuery([_URN_A, _URN_B])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, "")

        assert scope.resolved_urns == sorted([_URN_A, _URN_B])
        for column in ("origin", "platform_urn", "tag_urns", "glossary_term_urns"):
            assert f"dataset_registry.{column}" not in registry.sql, (
                f"an empty filter must not constrain {column}; got:\n{registry.sql}"
            )

    async def test_the_filter_predicate_is_pushed_into_the_query(self, db: AsyncMock) -> None:
        """The filter is evaluated in SQL, not by post-filtering a materialised list.

        spec: feature/BACKEND.md §Dataset resolution — the resolver "materialises no
            URN list where the caller can page in SQL".
        """
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        await resolve_dataset_scope(db, "'urn:li:tag:area:catalog' IN tag_urns")

        assert "dataset_registry.tag_urns" in registry.sql
        assert [value for value in registry.params.values() if value == ["urn:li:tag:area:catalog"]]

    async def test_every_literal_travels_as_a_bound_parameter(self, db: AsyncMock) -> None:
        """spec: feature/BACKEND.md §Dataset resolution — "**Every literal compiles
        to a bound parameter** […] so user filter text never reaches the database as
        SQL text"."""
        registry = _RegistryQuery([])
        registry.wire(db)

        await resolve_dataset_scope(db, "origin = 'PROD''; DROP TABLE dataset_registry; --'")

        assert "DROP TABLE" not in registry.sql
        assert "PROD'; DROP TABLE dataset_registry; --" in registry.params.values()

    async def test_a_stored_filter_that_does_not_parse_is_not_swallowed(
        self, db: AsyncMock
    ) -> None:
        """Filters are validated on every write path, so an unparseable stored filter
        is a defect signal rather than an empty scope.

        spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER is "Validated
            wherever a `dataset_filter` is written", so a stored filter that does not
            parse cannot arrive through the API. Silently resolving it to nothing would
            make a corrupted filter indistinguishable from one that matches nothing.
        """
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        with pytest.raises(DatasetFilterSyntaxError):
            await resolve_dataset_scope(db, "origin = ")

        assert registry.statements == [], "no query may be issued for an unparseable filter"


# ── Resolved and unresolved sets ─────────────────────────────────────────────


class TestResolvedSet:
    async def test_resolved_urns_are_sorted_and_deduplicated(self, db: AsyncMock) -> None:
        """A stable, duplicate-free scope: the run's dataset list is iterated,
        counted and reported, so the same registry state must yield the same list."""
        registry = _RegistryQuery([_URN_B, _URN_A, _URN_B])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, "")

        assert scope.resolved_urns == sorted({_URN_A, _URN_B})

    async def test_an_empty_registry_resolves_to_an_empty_scope(self, db: AsyncMock) -> None:
        registry = _RegistryQuery([])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, "origin = 'PROD'")

        assert scope.resolved_urns == []
        assert scope.unresolved_urns == []


class TestUnresolvedUrns:
    """spec: spec/API.md §`dataset_filter` grammar — "`dataset_urn` literals that
    match no registered dataset at run time are reported in the
    `METRIC.RUN_COMPLETE` event's `unresolved_urns` field"."""

    async def test_a_named_urn_the_registry_does_not_return_is_unresolved(
        self, db: AsyncMock
    ) -> None:
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, f"dataset_urn IN ('{_URN_A}', '{_URN_B}')")

        assert scope.resolved_urns == [_URN_A]
        assert scope.unresolved_urns == [_URN_B]

    async def test_a_named_urn_the_registry_returns_is_not_unresolved(
        self, db: AsyncMock
    ) -> None:
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, f"dataset_urn = '{_URN_A}'")

        assert scope.unresolved_urns == []

    async def test_a_filter_naming_no_urn_reports_nothing_unresolved(
        self, db: AsyncMock
    ) -> None:
        """Only URNs the operator named explicitly can go unresolved — an attribute
        filter that matches nothing is an answer, not an unresolved reference."""
        registry = _RegistryQuery([])
        registry.wire(db)

        scope = await resolve_dataset_scope(db, "origin = 'NOWHERE'")

        assert scope.unresolved_urns == []


class TestExplicitUrnOverride:
    """UC4's ``POST …/method/run {"dataset_urns": […]}`` narrows the conf's scope."""

    async def test_the_override_narrows_the_query(self, db: AsyncMock) -> None:
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        await resolve_dataset_scope(db, "origin = 'PROD'", explicit_urns_override=[_URN_A])

        assert "dataset_registry.dataset_urn IN" in registry.sql, (
            f"the override must be pushed into the query; got:\n{registry.sql}"
        )
        assert "dataset_registry.origin" in registry.sql, (
            "the override narrows the conf's filter rather than replacing it; the "
            f"filter predicate must survive. Got:\n{registry.sql}"
        )

    async def test_an_override_urn_out_of_scope_is_reported_unresolved(
        self, db: AsyncMock
    ) -> None:
        """A URN the operator named that the conf's filter does not cover comes back
        as unresolved rather than silently widening the run's scope."""
        registry = _RegistryQuery([_URN_A])
        registry.wire(db)

        scope = await resolve_dataset_scope(
            db, "origin = 'PROD'", explicit_urns_override=[_URN_A, _URN_C]
        )

        assert scope.resolved_urns == [_URN_A]
        assert scope.unresolved_urns == [_URN_C]


# ── The clause export ────────────────────────────────────────────────────────


class TestDatasetFilterClause:
    """spec: feature/BACKEND.md §Dataset resolution — the resolver "also exports the
    compiled clause on its own, so per-conf and per-metric dataset views push the
    filter into their own paginated query rather than slicing a resolved list in
    Python"."""

    def test_an_empty_filter_compiles_to_an_unconditional_true(self) -> None:
        clause = dataset_filter_clause("")
        assert str(clause.compile(dialect=postgresql.dialect())).strip().lower() == "true"

    def test_the_clause_carries_no_registry_restriction_of_its_own(self) -> None:
        """Callers add ``datahub_registered`` themselves, so the clause must not —
        a doubled restriction would be silently redundant, and a missing one on the
        caller's side must be visible as a bug in that caller."""
        clause = dataset_filter_clause("origin = 'PROD'")
        sql = str(clause.compile(dialect=postgresql.dialect()))
        assert "datahub_registered" not in sql
        assert "dataset_registry.origin" in sql

    def test_an_unparseable_filter_raises_rather_than_matching_everything(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError):
            dataset_filter_clause("origin = ")


# ── Service-layer validation ─────────────────────────────────────────────────


class TestServiceValidation:
    """Services validate independently of the request schemas because internal
    callers (activity endpoints, bootstrap) reach them without one."""

    def test_a_valid_filter_passes(self) -> None:
        validate_dataset_filter_service("origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns")

    def test_a_malformed_filter_raises_the_filter_code(self) -> None:
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            validate_dataset_filter_service("owner = 'alice'")
        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"

    def test_a_malformed_dataset_urn_literal_raises_the_urn_code(self) -> None:
        with pytest.raises(InvalidDatasetUrnError) as excinfo:
            validate_dataset_filter_service("dataset_urn = 'not-a-urn'")
        assert excinfo.value.error_code == "INVALID_DATASET_URN"

    def test_the_empty_filter_passes(self) -> None:
        validate_dataset_filter_service("")
        validate_dataset_filter_service(None)
