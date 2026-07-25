"""Route-catalogue conformance: registered FastAPI routes ↔ ``spec/API.md`` route tables.

``spec/API.md`` is priority-1 in the spec hierarchy and its §Route Catalogue is the
contract for what the API exposes. This module asserts both directions of that
contract, so that a route added to the app without a spec row — or a spec row whose
route was renamed or removed — fails a test run.

Spec: spec/API.md §Route Catalogue ("All routes are prefixed with `/api/v1`.")
Spec: spec/API.md §Route Catalogue → System ("System routes are mounted at the root
(`/health`, `/ready`) … the only documented exception to the `/api/v1` prefix convention.")
Spec: spec/TESTING.md §Assertion Discipline ("Author assertions so that a passing result
is only reachable when the spec'd behavior actually occurred.")

The two-directional treatment of the ``KNOWN_DRIFT_*`` allowlists below is a design
choice of this package rather than a rule quoted from a spec; ``assert_drift_allowlist``
implements it and ``test_drift_allowlist.py`` proves both of its branches fire.

Unit-tier: reads ``spec/API.md`` and imports the app object. No dev environment.
"""

from src.api.main import app

from ._api_md import (
    RouteRef,
    assert_drift_allowlist,
    registered_routes,
    spec_route_rows,
    spec_tbd_route_rows,
)

# ── Known-intentional exemptions from the catalogue ──────────────────────────
#
# spec/API.md §Internal Activities (`/internal/activities`): "The per-domain route
# shapes are not catalogued in this spec — they are an implementation detail of the
# workflow boundary and live with the relevant feature service in BACKEND.md."
UNCATALOGUED_PREFIXES: tuple[str, ...] = ("/internal/activities/",)

# Registered by FastAPI itself for its generated documentation, not by DataSpoke, and
# therefore outside the DataSpoke route contract in spec/API.md §Route Catalogue.
FRAMEWORK_PATHS: frozenset[str] = frozenset({"/openapi.json", "/redoc"})

# ── Known drift ──────────────────────────────────────────────────────────────
#
# Both directions of the route comparison are currently clean, so both allowlists are
# empty. They are declared (rather than omitted) so that a future author records a
# mismatch here — with the issue-#86 phase that resolves it — instead of loosening the
# assertion. `assert_drift_allowlist` fails on a stale entry too, so an allowlisted
# route that stops drifting must be deleted from the list.
KNOWN_DRIFT_REGISTERED_NOT_IN_SPEC: frozenset[str] = frozenset()
KNOWN_DRIFT_SPEC_NOT_REGISTERED: frozenset[str] = frozenset()


def _catalogued(routes: frozenset[RouteRef]) -> frozenset[RouteRef]:
    """Drop the routes spec/API.md intentionally declines to catalogue."""
    return frozenset(
        (method, path)
        for method, path in routes
        if not path.startswith(UNCATALOGUED_PREFIXES) and path not in FRAMEWORK_PATHS
    )


def _fmt(routes: frozenset[RouteRef]) -> set[str]:
    return {f"{method} {path}" for method, path in routes}


class TestRouteWalk:
    """Backstops proving the route walk and the exemption lists are not vacuous.

    Every assertion below guards a way the conformance test could pass while comparing
    almost nothing: a walk that stops at the lazily-wrapped included routers, an
    emptiness check that drops collection roots, or an exemption list that has gone
    stale and now excludes nothing.
    """

    def test_walk_descends_into_included_routers(self) -> None:
        """The recursive walk must reach endpoint routes, not just router wrappers.

        ``app.routes`` holds 14 entries, 12 of which are lazy include-wrappers carrying
        no ``path`` of their own — their endpoints live on ``original_router``. A
        non-recursive walk therefore yields exactly 2 routes (``/openapi.json`` and
        ``/redoc``); the real surface is over a hundred.

        The bound is a **floor at the current count**, not a loose sanity check: raise
        it when routes are added, and lower it only as a deliberate decision.
        """
        routes = registered_routes(app)
        assert len(routes) >= 120, (
            f"Only {len(routes)} routes walked — the walk is not descending into "
            f"included routers, so the conformance comparison is near-empty."
        )

    def test_collection_root_paths_are_registered(self) -> None:
        """A route declared as ``@router.get("")`` must survive normalisation.

        ``GET /spoke/common/data`` is the collection root from spec/API.md §Data
        Resource ("The collection root `GET /spoke/common/data` lists every registered
        dataset"). Its endpoint declares ``path == ""``, so a truthiness check on the
        path would silently drop it from the comparison.
        """
        assert ("GET", "/spoke/common/data") in registered_routes(app)

    def test_root_mounted_system_routes_are_registered(self) -> None:
        """§System routes live at the root, outside the ``/api/v1`` prefix."""
        routes = registered_routes(app)
        assert ("GET", "/health") in routes
        assert ("GET", "/ready") in routes

    def test_uncatalogued_prefix_exemption_matches_real_routes(self) -> None:
        """The ``/internal/activities`` exemption must still exclude something.

        If the prefix is renamed, this exemption would silently stop applying and the
        exempted routes would start failing the conformance test for the wrong reason.
        """
        exempted = {
            (method, path)
            for method, path in registered_routes(app)
            if path.startswith(UNCATALOGUED_PREFIXES)
        }
        assert exempted, (
            f"No registered route matches {UNCATALOGUED_PREFIXES} — the exemption is "
            f"dead and must be removed or corrected."
        )

    def test_framework_path_exemptions_match_real_routes(self) -> None:
        """Every FastAPI-owned path exemption must correspond to a registered route."""
        registered_paths = {path for _, path in registered_routes(app)}
        unmatched = sorted(FRAMEWORK_PATHS - registered_paths)
        assert not unmatched, (
            f"FRAMEWORK_PATHS entries match no registered route: {unmatched}. Delete "
            f"them — an exemption for a route that does not exist excludes nothing."
        )


class TestSpecRouteTableParsing:
    """Backstops proving the ``spec/API.md`` side of the comparison is not vacuous."""

    def test_spec_rows_are_parsed(self) -> None:
        """Floor at the current row count — raise it as the catalogue grows."""
        rows = spec_route_rows()
        assert len(rows) >= 120, (
            f"Only {len(rows)} route rows parsed from spec/API.md §Route Catalogue — "
            f"the table format likely changed and the parser is silently matching little."
        )

    def test_tbd_section_rows_are_parsed_and_excluded(self) -> None:
        """TBD sections must be parsed as rows *and* kept out of the catalogue.

        spec/API.md §"Redefined DataHub Functions *(TBD)*" states its routes are "**not
        yet defined**". Asserting the rows exist proves the exclusion is doing work —
        otherwise a parser that found no TBD rows at all would pass this check too.
        """
        tbd = spec_tbd_route_rows()
        assert tbd, (
            "No rows parsed from a TBD-flagged section of spec/API.md — the exclusion "
            "is not exercised, so it proves nothing."
        )
        leaked = sorted(_fmt(tbd & spec_route_rows()))
        assert not leaked, f"TBD rows leaked into the catalogue: {leaked}"


class TestRouteCatalogueConformance:
    """Every registered route is documented, and every documented route is registered.

    Spec: spec/API.md §Route Catalogue.
    """

    def test_every_registered_route_is_documented(self) -> None:
        undocumented = _fmt(_catalogued(registered_routes(app)) - spec_route_rows())
        assert_drift_allowlist(
            undocumented,
            KNOWN_DRIFT_REGISTERED_NOT_IN_SPEC,
            what="route registered on the app but absent from spec/API.md",
            allowlist_name="KNOWN_DRIFT_REGISTERED_NOT_IN_SPEC",
        )

    def test_every_documented_route_is_registered(self) -> None:
        unimplemented = _fmt(spec_route_rows() - _catalogued(registered_routes(app)))
        assert_drift_allowlist(
            unimplemented,
            KNOWN_DRIFT_SPEC_NOT_REGISTERED,
            what="route documented in spec/API.md but not registered on the app",
            allowlist_name="KNOWN_DRIFT_SPEC_NOT_REGISTERED",
        )
