"""Time-range query-param conformance: every ``event``/``result`` read takes ``from``/``to``.

The route-catalogue check in this package compares route **paths** only, so a route can
match its ``spec/API.md`` row exactly while declining to read a query param the spec says
it supports. FastAPI drops an unknown query param silently, so such a route answers 200
with an unfiltered body and no test notices. This module closes the **declaration** half of
that drift, for the one query-param rule ``spec/API.md`` states as a **class-level**
invariant over whole route families — which is what makes it checkable without per-route
prose parsing.

**The residual.** Declaration is not behaviour: reading the signature cannot distinguish a
route that declares ``from``/``to`` and filters on them from one that declares them and
never reads them. That failure mode is not hypothetical — the same change this module
accompanies also deleted a declared-but-unread ``cursor`` param. Behaviour is covered a
tier down, per route, by tests that fire the wire names and assert the resulting SQL or
response (e.g.
``tests/unit/api/routers/spoke/test_metagen.py::test_event_routes_filter_on_from_and_to_inclusively``
for the two metagen event feeds).

Spec: spec/API.md §Meta-Classifier Conventions → ``event`` — "Immutable history log of
occurrences on a resource. Always ``GET``; supports ``offset``/``limit`` pagination and
``sort=occurred_at_desc`` (default order, newest first). Supports ``from``/``to`` for
time-range filtering. Sub-paths may be defined in feature specs to narrow by outcome
(e.g. ``.../event/failure``, ``.../event/success``), but the parent ``.../event`` path
must remain and return all event types."
Spec: spec/API.md §Meta-Classifier Conventions → ``attr`` — "**Result attributes**
(``attr/<feat>/result``, ``attr/result``): periodic measurement records — use ``GET`` to
read (supports ``?from=…&to=…``, ``?latest=true``, and feature-specific filters)."
Spec: spec/API.md §Query Parameters — ``from`` is "Start of time-range filter, inclusive;
used on ``result`` and ``event`` endpoints"; ``to`` is "End of time-range filter,
inclusive; used on ``result`` and ``event`` endpoints. Optional — omitting it leaves the
range unbounded above, so the filter reaches the newest record".

**Why the expectation is derived, not parsed.** Only 11 of the 126 rows in §Route Catalogue
name a query param inline, and just two of those name a time-range bound — the ``until``
deviation allowlisted below, and the governance metric ``result`` read — so per-route
parsing cannot express a rule that binds a whole route *family*. The two meta-classifier
sentences above are that family-wide rule — every ``event`` route, every ``result``
attribute read — and this module applies it to the live app, with an explicit allowlist for
the deviations ``spec/API.md`` itself documents. (Issue #87 is adjacent but does not close
this gap: it proposes a machine-parseable representation of response *shapes*, not of query
params.)

**Wire names, not signatures.** A param declared ``Query(alias="from")`` is reachable only
as ``from``; the Python name (``from_time``) never appears on the wire. The check reads the
alias for exactly that reason, and :class:`TestQueryParamExtraction` pins the extraction
against ``app.openapi()`` so it cannot drift from the published contract.

Unit-tier: imports the app object and reads ``spec/API.md``. No dev environment.
"""

from collections import defaultdict

from src.api.main import app

from ._api_md import (
    RouteRef,
    assert_drift_allowlist,
    normalise_path,
    registered_route_query_params,
    registered_routes,
)

#: The two params §Query Parameters binds to ``result`` and ``event`` endpoints.
TIME_RANGE_PARAMS: tuple[str, ...] = ("from", "to")

# ── Known drift / documented deviations ──────────────────────────────────────
#
# Entries are formatted by ``_fmt_missing`` below. ``assert_drift_allowlist`` is
# bidirectional: an entry that stops drifting fails with an instruction to delete it, so a
# route that later gains its missing param forces its entry out of this list.
KNOWN_DRIFT_MISSING_TIME_RANGE: frozenset[str] = frozenset(
    {
        # spec/API.md §Route Catalogue → Data Resource, the row for this route: "`?from=…
        # &until=…&limit=…` — this endpoint names its end-bound param `until` rather than
        # the convention table's `to`". A documented exception, not a scheduled fix: the
        # route reads an end bound, under another name.
        # TestDocumentedUntilDeviation below proves it really declares `from`/`until`.
        "GET /spoke/common/data/{}/attr/validation/result (missing: to)",
    }
)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def is_event_route(method: str, path: str) -> bool:
    """Does this route serve the ``event`` meta-classifier sub-resource?

    ``event`` is a path *segment*, so both the parent ``.../event`` and its documented
    outcome sub-paths (``.../event/ingestion``, ``.../event/validation``) match, while an
    unrelated path that merely contains the letters (``/spoke/metagen/events-export``)
    does not. Restricted to ``GET`` because the classifier is "Always ``GET``".
    """
    return method == "GET" and "event" in _segments(path)


def is_result_attr_route(method: str, path: str) -> bool:
    """Does this route serve a ``result`` **attribute** read (``attr/result``)?

    The spec names the shapes explicitly — ``attr/<feat>/result`` and ``attr/result`` — so
    the match requires a preceding ``attr`` segment and ``result`` as the final segment.
    That excludes ``/spoke/ontogen/result/node``, where ``result`` heads a candidate
    *resource* path rather than a measurement attribute, and excludes ``POST
    .../attr/validation/result`` (result appends), since the ``?from=…&to=…`` support is
    stated for the ``GET`` read.
    """
    segments = _segments(path)
    return method == "GET" and "attr" in segments and segments[-1:] == ["result"]


def time_ranged_routes() -> frozenset[RouteRef]:
    """Every registered route the meta-classifier rule binds ``from``/``to`` on."""
    return frozenset(
        (method, path)
        for method, path in registered_routes(app)
        if is_event_route(method, path) or is_result_attr_route(method, path)
    )


def query_params_by_route() -> dict[RouteRef, frozenset[str]]:
    """``{route: declared query-param wire names}``, unioned over normalisation collisions.

    Two routes differing only in a path-parameter name collapse to one
    :data:`RouteRef`; unioning keeps the check honest for the identity it can express,
    and :meth:`TestRouteClassification.test_no_time_ranged_route_collides_on_its_ref`
    proves no in-scope route is actually in that situation today.
    """
    merged: dict[RouteRef, set[str]] = defaultdict(set)
    for ref, params in registered_route_query_params(app):
        merged[ref] |= set(params)
    return {ref: frozenset(params) for ref, params in merged.items()}


def _fmt_missing(method: str, path: str, missing: tuple[str, ...]) -> str:
    return f"{method} {path} (missing: {', '.join(missing)})"


def missing_time_range_params() -> set[str]:
    """Formatted entries for in-scope routes that do not declare ``from`` and ``to``."""
    declared = query_params_by_route()
    entries: set[str] = set()
    for method, path in time_ranged_routes():
        params = declared.get((method, path), frozenset())
        missing = tuple(name for name in TIME_RANGE_PARAMS if name not in params)
        if missing:
            entries.add(_fmt_missing(method, path, missing))
    return entries


class TestRouteClassification:
    """Backstops proving the two classifiers select real, non-empty, correct route sets.

    Without these, a classifier that matched nothing would make the conformance assertion
    below pass over an empty set, and a classifier that over-matched would fail for the
    wrong reason.
    """

    def test_event_classifier_discriminates(self) -> None:
        # Positive: the parent path and its outcome sub-paths.
        assert is_event_route("GET", "/spoke/metagen/event")
        assert is_event_route("GET", "/spoke/common/data/{}/event")
        assert is_event_route("GET", "/spoke/common/data/{}/event/metagen")
        # Negative: not a `GET`, not the `event` segment, not an event route at all.
        assert not is_event_route("POST", "/spoke/metagen/event")
        assert not is_event_route("GET", "/spoke/metagen/events-export")
        assert not is_event_route("GET", "/spoke/metagen/conf/{}")

    def test_result_attr_classifier_discriminates(self) -> None:
        # Positive: both shapes §Meta-Classifier Conventions names.
        assert is_result_attr_route("GET", "/spoke/governance/metric/{}/attr/result")
        assert is_result_attr_route("GET", "/spoke/common/data/{}/attr/validation/result")
        # Negative: `result` heading a resource path, not an `attr` measurement read.
        assert not is_result_attr_route("GET", "/spoke/ontogen/result/node")
        assert not is_result_attr_route("GET", "/spoke/ontogen/result/node/{}")
        # Negative: the append, whose spec'd support is on the `GET` read.
        assert not is_result_attr_route("POST", "/spoke/common/data/{}/attr/validation/result")

    def test_in_scope_set_covers_both_families(self) -> None:
        """The rule must bind a substantial, non-empty set drawn from both families."""
        in_scope = time_ranged_routes()
        events = {ref for ref in in_scope if is_event_route(*ref)}
        results = {ref for ref in in_scope if is_result_attr_route(*ref)}
        # Floors at the current counts (12 event routes, 2 result reads) — raise them as
        # routes are added, so a classifier that silently stops matching one is caught.
        assert len(events) >= 12, f"Only {len(events)} event routes classified: {sorted(events)}"
        assert len(results) >= 2, f"Only {len(results)} result reads classified: {sorted(results)}"

    def test_both_metagen_event_routes_are_in_scope(self) -> None:
        """The per-conf and cross-conf metagen feeds are subject to the rule.

        spec/feature/FRONTEND_METAGEN.md §Conf create / detail gives the per-conf event
        table "a `datetime` [RangePicker](FRONTEND_BASIC.md#shared-component-notes)
        driving `from`/`to`", and §Components gives `MetagenEventTable` — bound to
        both the conf-detail and cross-conf feeds — the same pairing. Named explicitly so a
        classifier that quietly stopped matching them cannot hide their omission behind the
        floors above.
        """
        in_scope = time_ranged_routes()
        assert ("GET", "/spoke/metagen/conf/{}/event") in in_scope
        assert ("GET", "/spoke/metagen/event") in in_scope

    def test_no_time_ranged_route_collides_on_its_ref(self) -> None:
        """No two in-scope routes normalise to the same ref, so the union above is a no-op."""
        counts: dict[RouteRef, int] = defaultdict(int)
        for ref, _ in registered_route_query_params(app):
            counts[ref] += 1
        in_scope = time_ranged_routes()
        collisions = sorted(
            f"{m} {p}" for (m, p), n in counts.items() if n > 1 and (m, p) in in_scope
        )
        assert not collisions, (
            f"In-scope routes collide on their normalised ref: {collisions}. Their param "
            f"sets are merged, so a missing param on one would be masked by the other."
        )


class TestQueryParamExtraction:
    """Backstops proving the param extraction reports real wire names.

    The whole point of the conformance check is that a param is reachable only under the
    name a client sends. If the extraction reported Python parameter names, every route
    would read as missing `from`/`to` (they are declared `from_time`/`to_time`), and if it
    reported nothing at all the allowlist comparison would degenerate.
    """

    def test_aliases_are_reported_not_python_names(self) -> None:
        params = query_params_by_route()[("GET", "/spoke/metagen/event")]
        assert "from" in params and "to" in params, (
            f"Alias wire names missing from the extraction: {sorted(params)}"
        )
        assert "from_time" not in params and "to_time" not in params, (
            f"Python parameter names leaked into the extraction: {sorted(params)}"
        )

    def test_extraction_matches_the_published_openapi_contract(self) -> None:
        """Per in-scope route, the walked params equal the ones ``app.openapi()`` publishes.

        ``app.openapi()`` is the client-facing statement of the wire names. Pinning the
        walk against it covers both directions of extraction error: a param the walk
        invents, and one it misses (e.g. supplied by a shared ``Depends`` provider).
        """
        published: dict[RouteRef, set[str]] = defaultdict(set)
        for raw_path, operations in app.openapi()["paths"].items():
            ref_path = normalise_path(raw_path)
            for raw_method, operation in operations.items():
                method = raw_method.upper()
                if method in ("HEAD", "OPTIONS"):
                    continue
                published[(method, ref_path)] |= {
                    parameter["name"]
                    for parameter in operation.get("parameters", ())
                    if parameter.get("in") == "query"
                }

        walked = query_params_by_route()
        in_scope = sorted(time_ranged_routes())
        assert in_scope, "No in-scope route to compare — the classifiers matched nothing."
        for ref in in_scope:
            assert walked.get(ref) == frozenset(published.get(ref, set())), (
                f"{ref[0]} {ref[1]}: walked query params {sorted(walked.get(ref, ()))} "
                f"differ from the openapi contract {sorted(published.get(ref, ()))}"
            )


class TestTimeRangeParamConformance:
    """Every ``event`` and ``result`` read declares both ``from`` and ``to``.

    Spec: spec/API.md §Meta-Classifier Conventions; §Query Parameters (quoted in the module
    docstring).
    """

    def test_every_event_and_result_route_accepts_from_and_to(self) -> None:
        assert_drift_allowlist(
            missing_time_range_params(),
            KNOWN_DRIFT_MISSING_TIME_RANGE,
            what=(
                "`event`/`result` route that does not declare the `from`/`to` time-range "
                "params spec/API.md §Meta-Classifier Conventions binds to its family"
            ),
            allowlist_name="KNOWN_DRIFT_MISSING_TIME_RANGE",
        )


class TestDocumentedUntilDeviation:
    """The one allowlisted route reads an end bound — under the name the spec documents.

    Without this, the allowlist entry would be indistinguishable from "this route reads no
    end bound at all", which is the very defect the conformance check exists to catch.
    """

    def test_validation_result_read_declares_from_and_until(self) -> None:
        """Spec: spec/API.md §Route Catalogue → Data Resource — ``?from=…&until=…&limit=…``."""
        params = query_params_by_route()[
            ("GET", "/spoke/common/data/{}/attr/validation/result")
        ]
        assert "from" in params, f"Start bound missing: {sorted(params)}"
        assert "until" in params, (
            f"The documented `until` end bound is missing: {sorted(params)}. If this route "
            f"was migrated to `to`, delete its KNOWN_DRIFT_MISSING_TIME_RANGE entry and "
            f"update the spec/API.md row that names this endpoint's end-bound param "
            f"`until` rather than the convention table's `to`."
        )
