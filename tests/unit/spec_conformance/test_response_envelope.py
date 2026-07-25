"""Envelope conformance: paginated schemas ↔ ``spec/API.md §Standard Response Envelope``.

§Standard Response Envelope states: "All collection responses include a content key named
after the resource + pagination metadata". Every collection route in the API is served by a
``PaginatedResponse`` subclass, so that sentence is checkable against the schema layer
directly: a subclass may add its content key and nothing else.

Two rules are enforced here, and it is worth being exact about what each does and does not
deliver, because the bug that prompted this module — a scalar aggregate on a response model
shared by two routes, permanently zero on the route that never populated it — is only
caught by the two of them together.

1. **Envelope shape** (:class:`TestResponseEnvelopeConformance`): every paginated model is
   one list-typed content key plus the pagination envelope; any additional field must be
   allowlisted with the spec text documenting it. The allowlist key is
   ``ClassName.field_name`` — a *model*, with no route binding. On its own this rule is
   therefore weaker than it looks: a field documented for route A, on a model that also
   serves route B, is allowlistable and passes. Run against the pre-fix tree it would have
   surfaced ``MetagenItemListResponse.candidate_count`` exactly once, the author would have
   found it documented at API.md §Route Catalogue → Data Resource, allowlisted it, and gone
   green with the always-zero field intact.
2. **Route binding** (:class:`TestSharedModelBinding`): that residual is what the second
   rule closes. A model carrying an envelope aggregate beyond the content key may serve
   **one** route, because such an aggregate is defined per route; and the set of models
   serving more than one route is itself pinned, so new sharing must be declared. The
   pre-fix tree fails this rule no matter what the first rule's allowlist says.

What is asserted mechanically is the *shape* of the envelope rule — exactly one list-typed
content field, plus pagination. The "named after the resource" half is not machine checkable
without restating each route's vocabulary, so it is deliberately left to the per-route tests
rather than approximated here.

Spec: spec/API.md §Standard Response Envelope ("All collection responses include a content
key named after the resource + pagination metadata"; the example enumerates ``offset``,
``limit``, ``total_count``, ``resp_time``).
Spec: spec/TESTING.md §Assertion Discipline ("Author assertions so that a passing result is
only reachable when the spec'd behavior actually occurred.")

The bidirectional treatment of ``DOCUMENTED_ENVELOPE_AGGREGATES`` is the same design choice
the rest of this package makes: ``assert_drift_allowlist`` fails both on an undeclared extra
field and on a declared one that no longer exists, and ``test_drift_allowlist.py`` proves
both of its branches fire.

Unit-tier: imports schema modules and parses ``src/`` with ``ast``. No dev environment.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_origin

from src.api.main import app
from src.api.schemas.common import PaginatedResponse

from ._api_md import (
    api_schema_module_names,
    assert_drift_allowlist,
    declared_paginated_subclass_defs,
    registered_route_response_models,
    src_python_roots,
)

#: The pagination envelope every collection response carries, per the example in
#: spec/API.md §Standard Response Envelope. Declared here rather than read from
#: ``PaginatedResponse`` so the two can be compared — see
#: ``TestEnvelopeBaseFields.test_base_model_matches_the_specified_envelope``.
ENVELOPE_FIELDS: frozenset[str] = frozenset({"offset", "limit", "total_count", "resp_time"})

#: Non-content fields a paginated response is documented to carry *in addition* to the
#: standard envelope, as ``ClassName.field_name``.
#:
#: ``MetagenItemListResponse.candidate_count`` is documented for the per-dataset item route
#: in spec/API.md §Route Catalogue → Data Resource: "The response envelope also carries a
#: dataset-level `candidate_count` — total candidates of any status for this dataset … —
#: distinct from `total_count` (the item count)". It is dataset-scoped, so it is defined for
#: that route only; the cross-dataset index (`GET /spoke/metagen/item`) is served by the
#: sibling ``MetagenItemIndexResponse``, whose row inventory in §Route Catalogue → Metadata
#: Generation names no envelope aggregate.
#:
#: An entry here must quote the spec text that documents the field. A field that is merely
#: convenient does not belong: put it on the rows, or give the route its own model.
DOCUMENTED_ENVELOPE_AGGREGATES: frozenset[str] = frozenset(
    {
        "MetagenItemListResponse.candidate_count",
    }
)


def _paginated_models() -> dict[str, type[PaginatedResponse]]:
    """``{ClassName: model}`` for every ``PaginatedResponse`` subclass in ``src/api/schemas``.

    Only classes *defined* in the module being scanned are collected (``__module__``
    check), so a class re-exported through another schema module is counted once and the
    uniqueness backstop below stays meaningful.
    """
    models: dict[str, type[PaginatedResponse]] = {}
    for module_name in api_schema_module_names():
        module = importlib.import_module(module_name)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, PaginatedResponse)
                and obj is not PaginatedResponse
                and obj.__module__ == module_name
            ):
                models[obj.__name__] = obj
    return models


#: Paginated response models legitimately bound to more than one route, as
#: ``ClassName``. Sharing is admissible only for a model whose envelope is the content key
#: plus pagination and nothing else — a model carrying a route-scoped aggregate must serve
#: exactly one route, which ``TestSharedModelBinding`` enforces independently of this list.
#:
#: ``EventListResponse`` — the per-feature and per-resource event timelines are the same
#: resource rendered from the same rows; spec/API.md §Route Catalogue gives every ``event``
#: route an identical row inventory.
#: ``ApiTokenListResponse`` — ``GET /auth/api-tokens`` (own tokens) and
#: ``GET /admin/users/{user_id}/api-tokens`` (another user's) differ in scope, not in shape.
SHARED_PAGINATED_MODELS: frozenset[str] = frozenset(
    {
        "EventListResponse",
        "ApiTokenListResponse",
    }
)


def _route_bindings() -> dict[str, list[str]]:
    """``{ClassName: ["GET /path", …]}`` for every route whose ``response_model`` is paginated."""
    bindings: dict[str, list[str]] = {}
    for (method, path), model in registered_route_response_models(app):
        if isinstance(model, type) and issubclass(model, PaginatedResponse):
            bindings.setdefault(model.__name__, []).append(f"{method} {path}")
    return bindings


def _extra_fields(model: type[PaginatedResponse]) -> set[str]:
    return set(model.model_fields) - ENVELOPE_FIELDS


def _content_fields(model: type[PaginatedResponse]) -> set[str]:
    """Extra fields whose annotation is a ``list[...]`` — the candidate content keys."""
    return {
        name
        for name in _extra_fields(model)
        if get_origin(model.model_fields[name].annotation) is list
    }


class TestEnvelopeBaseFields:
    """The envelope constant this module compares against must be the real one."""

    def test_base_model_matches_the_specified_envelope(self) -> None:
        """``PaginatedResponse`` carries exactly the four documented envelope keys.

        This does two things, neither of which is "stop the suite going quiet" — because it
        would not: ``_extra_fields`` subtracts the ``ENVELOPE_FIELDS`` constant rather than
        the base model's own fields, so a field added to ``PaginatedResponse`` is already
        reported as an extra field on all 22 subclasses.

        What it does deliver: (1) it turns those 22 near-identical failures into one
        failure that names the actual cause, and (2) it pins ``ENVELOPE_FIELDS`` to the
        implementation, so the constant this module measures everything against cannot
        quietly diverge from the envelope the API really serves.

        Spec: spec/API.md §Standard Response Envelope — the collection example carries
        ``offset``, ``limit``, ``total_count``, ``resp_time`` alongside the content key.
        """
        assert set(PaginatedResponse.model_fields) == ENVELOPE_FIELDS, (
            f"PaginatedResponse fields {sorted(PaginatedResponse.model_fields)} no longer "
            f"match the envelope documented in spec/API.md §Standard Response Envelope "
            f"{sorted(ENVELOPE_FIELDS)}. Update the spec and this constant together."
        )


class TestPaginatedModelDiscovery:
    """Backstops proving the conformance check below inspects a real, complete set.

    Each guards a way the check could pass while examining almost nothing: an import walk
    that finds no models, a name collision that drops one, or a model defined outside the
    schemas package and therefore never imported here.
    """

    def test_models_are_discovered(self) -> None:
        """Floor at the current count — raise it as collection routes are added."""
        models = _paginated_models()
        assert len(models) >= 22, (
            f"Only {len(models)} PaginatedResponse subclasses discovered under "
            f"src/api/schemas — the import walk is finding almost nothing, so the "
            f"envelope conformance assertions below are near-vacuous."
        )

    def test_schema_module_names_resolve_to_importable_modules(self) -> None:
        """The walked package must be the schemas directory, and every module importable.

        The walk runs over the *imported* ``src.api.schemas`` package, so it is pinned back
        to the filesystem root the AST-based helpers use. Without this, a package moved or
        shadowed elsewhere on ``sys.path`` would be walked happily while the AST scan read
        a different tree, and the two halves of the discovery backstop would stop
        corroborating each other.
        """
        import src.api.schemas as schemas_pkg

        walked = {Path(entry).resolve() for entry in schemas_pkg.__path__}
        assert walked == {(src_python_roots()["api"] / "schemas").resolve()}, (
            f"src.api.schemas resolves to {walked}, not the src/api/schemas directory the "
            f"AST helpers scan — module discovery and the AST scan are reading different "
            f"trees."
        )
        names = api_schema_module_names()
        assert "src.api.schemas.metagen" in names, (
            f"src/api/schemas/metagen.py is not in the discovered module list {names} — "
            f"the schemas package layout changed and the discovery helper is stale."
        )
        for name in names:
            importlib.import_module(name)

    def test_class_names_are_unique_across_schema_modules(self) -> None:
        """Keying by bare class name is only sound while the names are unique.

        A duplicate would make one model overwrite the other in the discovery dict and
        silently drop it from the comparison — and would make an allowlist entry
        ambiguous about which class it excuses.
        """
        by_name: dict[str, list[str]] = {}
        for module_name in api_schema_module_names():
            module = importlib.import_module(module_name)
            for obj in vars(module).values():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, PaginatedResponse)
                    and obj is not PaginatedResponse
                    and obj.__module__ == module_name
                ):
                    by_name.setdefault(obj.__name__, []).append(module_name)
        duplicated = {name: mods for name, mods in by_name.items() if len(mods) > 1}
        assert not duplicated, (
            f"Paginated response class names collide across schema modules: {duplicated}. "
            f"Rename one, or key this module's discovery on the qualified name."
        )

    def test_every_directly_declared_subclass_lives_in_the_schemas_package(self) -> None:
        """No model that **directly** declares ``PaginatedResponse`` may sit outside the
        imported set.

        The AST scan covers every Python package under ``src/``, so a model declared in a
        router (or anywhere else) — which ``_paginated_models`` would never import — is
        failed here rather than missed.

        The match is syntactic and one level deep: ``Rogue(_Intermediate)`` where
        ``_Intermediate(PaginatedResponse)`` yields only ``_Intermediate``, and ``Rogue``
        escapes both this scan and the import walk. The count equality below is the canary
        for exactly that — it holds only while every discovered model is also a direct
        declaration, so an indirection (or a model reachable by import but not by the scan)
        breaks it and forces this backstop to be rewritten rather than silently narrowed.
        """
        declared = declared_paginated_subclass_defs("PaginatedResponse")
        assert declared, (
            "No class declaring PaginatedResponse as a base was found by the AST scan of "
            "src/ — the base class was likely renamed, and this backstop now proves nothing."
        )
        discovered = set(_paginated_models())
        stray = {
            module: [name for name in names if name not in discovered]
            for module, names in declared.items()
        }
        stray = {module: names for module, names in stray.items() if names}
        assert not stray, (
            f"PaginatedResponse subclasses declared outside the discovered schema set: "
            f"{stray}. Move them into src/api/schemas so the envelope rule covers them."
        )
        declared_count = sum(len(names) for names in declared.values())
        assert declared_count == len(discovered), (
            f"{declared_count} classes directly declare PaginatedResponse but "
            f"{len(discovered)} were imported from src/api/schemas. The two views have "
            f"diverged — most likely a model now inherits through an intermediate base, "
            f"which the AST scan cannot see. Widen the scan before trusting it again."
        )


class TestResponseEnvelopeConformance:
    """Every paginated response is one content key plus the pagination envelope.

    Spec: spec/API.md §Standard Response Envelope.
    """

    def test_every_paginated_model_has_exactly_one_content_key(self) -> None:
        """One list-typed field per collection response — no more, no fewer.

        Zero content fields means the route returns pagination metadata about nothing;
        two means the response is really two collections and the caller cannot tell which
        ``total_count`` describes which.
        """
        offenders = {
            name: sorted(_content_fields(model))
            for name, model in sorted(_paginated_models().items())
            if len(_content_fields(model)) != 1
        }
        assert not offenders, (
            f"Paginated responses without exactly one list-typed content key: {offenders}. "
            f"spec/API.md §Standard Response Envelope: 'All collection responses include a "
            f"content key named after the resource + pagination metadata.'"
        )

    def test_only_documented_aggregates_extend_the_envelope(self) -> None:
        """A scalar field beside the content key must be documented in spec/API.md.

        This is the assertion that keeps a per-route aggregate from being bolted onto a
        model shared by several routes, where every route that cannot populate it
        serialises the default forever.
        """
        undocumented = {
            f"{name}.{field}"
            for name, model in _paginated_models().items()
            for field in _extra_fields(model) - _content_fields(model)
        }
        assert_drift_allowlist(
            undocumented,
            DOCUMENTED_ENVELOPE_AGGREGATES,
            what=(
                "non-content field on a paginated response envelope "
                "(spec/API.md §Standard Response Envelope)"
            ),
            allowlist_name="DOCUMENTED_ENVELOPE_AGGREGATES",
        )


class TestSharedModelBinding:
    """Which routes each paginated model serves — the half the envelope rule cannot see.

    ``DOCUMENTED_ENVELOPE_AGGREGATES`` is keyed on the model, so it cannot distinguish a
    field documented for the one route its model serves from the same field riding along on
    a second route that never populates it. These tests supply that missing binding.
    """

    def test_route_bindings_are_discovered(self) -> None:
        """Backstop: the walk must actually find paginated routes to reason about.

        A walk returning nothing — a renamed ``response_model`` attribute, a route walk
        that stops at the include-wrappers — would make both assertions below pass while
        examining an empty mapping.
        """
        bindings = _route_bindings()
        assert len(bindings) >= 22, (
            f"Only {len(bindings)} paginated response models are bound to a route "
            f"({sorted(bindings)}) — the route walk is finding almost nothing, so the "
            f"binding assertions below are vacuous."
        )
        unbound = sorted(set(_paginated_models()) - set(bindings))
        assert not unbound, (
            f"Paginated models served by no route: {unbound}. Either the route walk misses "
            f"them or they are dead schemas — delete them or bind them."
        )

    def test_models_bound_to_several_routes_are_declared(self) -> None:
        """New sharing of a paginated model must be a deliberate, recorded decision.

        Sharing is not wrong in itself — an event timeline is the same resource wherever
        it is served. It is recorded because sharing is the precondition for the defect the
        next test rules out, so a model quietly acquiring a second route should be looked
        at rather than absorbed.
        """
        shared = {name for name, routes in _route_bindings().items() if len(routes) > 1}
        assert_drift_allowlist(
            shared,
            SHARED_PAGINATED_MODELS,
            what="paginated response model bound as the response_model of several routes",
            allowlist_name="SHARED_PAGINATED_MODELS",
        )

    def test_a_model_carrying_an_envelope_aggregate_serves_one_route(self) -> None:
        """An envelope aggregate is route-scoped, so its model may serve exactly one route.

        This is the assertion that would have failed on the pre-fix tree regardless of what
        the envelope allowlist said: one model carrying ``candidate_count`` was bound to
        both the per-dataset item route — which populates it — and the cross-dataset index,
        which cannot, and so served it as a permanent zero. No allowlist entry can excuse
        that combination, because the two rules are keyed on different things.

        Spec: spec/API.md §Route Catalogue → Data Resource — the dataset-level
        ``candidate_count`` is "total candidates of any status for this dataset", a measure
        that is only defined once a route has fixed which dataset it means.
        """
        bindings = _route_bindings()
        offenders = {
            name: {"aggregates": sorted(aggregates), "routes": sorted(bindings.get(name, []))}
            for name, model in sorted(_paginated_models().items())
            if (aggregates := _extra_fields(model) - _content_fields(model))
            and len(bindings.get(name, [])) > 1
        }
        assert not offenders, (
            f"Paginated models carrying a route-scoped envelope aggregate while serving "
            f"more than one route: {offenders}. A route that cannot populate the aggregate "
            f"serves it at its default forever. Split the model so each route gets the "
            f"envelope it can fill."
        )
