"""Shared ``dataset_filter`` resolution for backend services.

UC3 ontogen, UC4 metagen and UC5 metrics resolve scope through this one module.
Resolution is a **DataSpoke-side SQL query** over ``dataset_registry`` — the
local mirror of the DataHub estate refreshed by the sync sweep — not a DataHub
search, so a filter's scope is at most one sweep interval stale.

Two exports, and the difference matters:

- :func:`resolve_dataset_scope` materialises the URN list, for callers that need
  the set itself (a metric run, an ontogen run).
- :func:`dataset_filter_clause` hands back the compiled clause so a caller that
  already pages in SQL pushes the predicate into its own query instead of
  slicing a resolved list in Python.

Spec: spec/feature/BACKEND.md §Dataset resolution,
spec/API.md §``dataset_filter`` grammar.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.shared.dataset_filter import (
    check_dataset_urn_literals,
    filter_clause,
    literal_dataset_urns,
    parse_filter,
)
from src.shared.db.models import DatasetRegistry

__all__ = [
    "ResolvedDatasetScope",
    "dataset_filter_clause",
    "resolve_dataset_scope",
    "validate_dataset_filter_service",
]


@dataclass
class ResolvedDatasetScope:
    resolved_urns: list[str] = field(default_factory=list)
    unresolved_urns: list[str] = field(default_factory=list)


def dataset_filter_clause(dataset_filter: str | None) -> ColumnElement[bool]:
    """Compile *dataset_filter* to a boolean expression over ``dataset_registry``.

    The empty filter compiles to ``TRUE``. Callers add their own
    ``datahub_registered`` restriction — :func:`resolve_dataset_scope` and every
    paged view do.

    Raises:
        DatasetFilterSyntaxError: when the stored text does not parse. Filters
            are validated on every write path, so this is a defect signal rather
            than an expected outcome, and it is deliberately not swallowed.
    """
    return filter_clause(parse_filter(dataset_filter))


async def resolve_dataset_scope(
    db: AsyncSession,
    dataset_filter: str | None,
    *,
    explicit_urns_override: list[str] | None = None,
) -> ResolvedDatasetScope:
    """Resolve *dataset_filter* to the registered datasets it matches.

    One query: ``dataset_registry`` rows with ``datahub_registered = true`` AND
    the compiled filter clause. An empty filter is the bare registered set.

    ``explicit_urns_override`` **narrows** the result to the named URNs (UC4's
    ``POST …/method/run {"dataset_urns": […]}``); it never widens scope past the
    conf's own filter.

    ``unresolved_urns`` are the URNs the operator named that the query did not
    return — the filter's literal ``dataset_urn`` values plus any override URNs —
    preserving the run-complete event field's meaning.
    """
    ast = parse_filter(dataset_filter)

    stmt = select(DatasetRegistry.dataset_urn).where(
        DatasetRegistry.datahub_registered.is_(True),
        filter_clause(ast),
    )
    if explicit_urns_override is not None:
        stmt = stmt.where(DatasetRegistry.dataset_urn.in_(explicit_urns_override))

    rows = (await db.execute(stmt)).scalars().all()
    resolved = sorted(set(rows))
    resolved_set = set(resolved)

    named: list[str] = list(literal_dataset_urns(ast))
    if explicit_urns_override is not None:
        named.extend(urn for urn in explicit_urns_override if urn not in named)

    return ResolvedDatasetScope(
        resolved_urns=resolved,
        unresolved_urns=[urn for urn in named if urn not in resolved_set],
    )


def validate_dataset_filter_service(dataset_filter: str | None) -> None:
    """Validate a ``dataset_filter`` at the service layer.

    Services validate independently of the request schemas because internal
    callers (activity endpoints, bootstrap) reach them without one. Both
    exceptions already carry their spec error code and 422 status, so they
    propagate unchanged.
    """
    check_dataset_urn_literals(parse_filter(dataset_filter))
