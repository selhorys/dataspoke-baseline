"""Tests for src/shared/events.py — the event-type constant catalogue.

Spec traceability:
- spec/feature/BACKEND.md §Event Catalogue (L1602-1628) — the domain (`entity_type`) /
  action / trigger table this module's constants implement, plus the config-lifecycle
  prose (L1606-1613) naming which domains emit `CONFIG_CREATE`/`CONFIG_UPDATE`/
  `CONFIG_DELETE` and the explicit `VALIDATION` carve-out (no `CONFIG_DELETE`).
- spec/feature/BACKEND.md:1591 — "Event type values are **uppercase**, dot-delimited:
  `{DOMAIN}.{ACTION}`." — the naming-convention rule asserted below.
- spec/feature/BACKEND_SCHEMA.md §events, `event_type` column (L513) — "Uppercase,
  dot-delimited `{DOMAIN}.{ACTION}`" and "Full catalogue in BACKEND §Event Catalogue" —
  the single-string-column contract that makes per-domain uniqueness meaningful, and
  L537 — domain-level endpoints "filter by `event_type` prefix", the behavioral contract
  the `*_PREFIX` constants exist to serve.
"""

import re

import src.shared.events as events_module

_UPPERCASE_DOT_DELIMITED = re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)*\.[A-Z0-9]+(_[A-Z0-9]+)*$")

# The full spec/feature/BACKEND.md §Event Catalogue as an explicit expected set of
# "DOMAIN.ACTION" strings, transcribed by hand from the table (L1617-1628) and the
# config-lifecycle prose (L1606-1613) rather than derived from the module under test.
# A missing constant or an uncatalogued extra constant both fail set equality below,
# pointing back at this section to re-sync spec and code.
_EXPECTED_CATALOGUE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # INGESTION — config-lifecycle actions are named SOURCE_* instead of CONFIG_*
        # because its configuration resource is a source (L1607-1609).
        "INGESTION.SOURCE_CREATE",
        "INGESTION.SOURCE_UPDATE",
        "INGESTION.SOURCE_DELETE",
        "INGESTION.COMPLETE",
        "INGESTION.FAIL",
        # VALIDATION — CONFIG_CREATE/CONFIG_UPDATE only, explicitly no CONFIG_DELETE
        # (L1609-1613, L1622): deleting a validation conf hard-deletes the dataset's
        # validation events as part of its cascade.
        "VALIDATION.CONFIG_CREATE",
        "VALIDATION.CONFIG_UPDATE",
        "VALIDATION.RESULT_RECORDED",
        # METAGEN — full config-lifecycle triad plus run and candidate-review actions.
        "METAGEN.CONFIG_CREATE",
        "METAGEN.CONFIG_UPDATE",
        "METAGEN.CONFIG_DELETE",
        "METAGEN.RUN_COMPLETE",
        "METAGEN.RUN_FAILED",
        "METAGEN.CANDIDATE_APPROVE",
        "METAGEN.CANDIDATE_REJECT",
        # METRIC — full config-lifecycle triad plus run completion.
        "METRIC.CONFIG_CREATE",
        "METRIC.CONFIG_UPDATE",
        "METRIC.CONFIG_DELETE",
        "METRIC.RUN_COMPLETE",
        # ONTOGEN — full config-lifecycle triad (singleton conf) plus seed CRUD and
        # run actions.
        "ONTOGEN.CONFIG_CREATE",
        "ONTOGEN.CONFIG_UPDATE",
        "ONTOGEN.CONFIG_DELETE",
        "ONTOGEN.SEED_CREATE",
        "ONTOGEN.SEED_UPDATE",
        "ONTOGEN.SEED_DELETE",
        "ONTOGEN.RUN_COMPLETE",
        "ONTOGEN.RUN_FAILED",
        # NODE / EDGE / TRIPLE — review-only domains, APPROVE/REJECT actions.
        "NODE.APPROVE",
        "NODE.REJECT",
        "EDGE.APPROVE",
        "EDGE.REJECT",
        "TRIPLE.APPROVE",
        "TRIPLE.REJECT",
        # AUTH — four rows, all looked up by entity or exact event type rather than
        # a domain-level prefix endpoint (see the AUTH_PREFIX absence guard below).
        "AUTH.GOOGLE_UNBOUND",
        "AUTH.GOOGLE_LINK_CREDENTIAL_RESET",
        "AUTH.API_TOKEN_REVOKED",
        "AUTH.ROLE_SYNC_FIXED",
    }
)


def _public_constants() -> dict[str, str]:
    """All public module-level string constants (the event-type catalogue itself)."""
    return {
        name: value
        for name, value in vars(events_module).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def _prefix_constants() -> dict[str, str]:
    """The `*_PREFIX` subset, keyed by their domain (e.g. `INGESTION_PREFIX` -> `INGESTION`)."""
    return {
        name[: -len("_PREFIX")]: value
        for name, value in _public_constants().items()
        if name.endswith("_PREFIX")
    }


def _full_event_type_constants() -> dict[str, str]:
    """All full `DOMAIN.ACTION` constants, excluding the `*_PREFIX` constants."""
    return {
        name: value
        for name, value in _public_constants().items()
        if not name.endswith("_PREFIX")
    }


def _domain_event_constants(domain: str) -> dict[str, str]:
    """Full `DOMAIN_ACTION` constants for `domain`, excluding the domain's own `_PREFIX`
    constant."""
    prefix_name = f"{domain}_PREFIX"
    return {
        name: value
        for name, value in _public_constants().items()
        if name.startswith(f"{domain}_") and name != prefix_name
    }


# ── Catalogue conformance ─────────────────────────────────────────────────────


def test_module_event_types_match_the_spec_catalogue_exactly() -> None:
    """The module's full set of `"DOMAIN.ACTION"` constant values equals
    `_EXPECTED_CATALOGUE_EVENT_TYPES`, a hand-transcribed copy of the spec's §Event
    Catalogue table, kept in this file rather than parsed from the markdown.

    This mechanically catches the code-to-transcript direction: adding, removing,
    or renaming a constant in `src/shared/events.py` without updating this file's
    transcript fails here immediately (the drift class issue #189 was raised to
    fix, e.g. `AUTH.ROLE_SYNC_FIXED` existing in code before it had a spec row).
    It does **not** catch the spec-to-code direction — this test never opens
    `spec/feature/BACKEND.md`, so a table edit there with no matching transcript
    update here stays green. Keeping `_EXPECTED_CATALOGUE_EVENT_TYPES` in sync with
    the spec table is a manual obligation on whoever edits either side; this test
    only guards that the module and the transcript agree with each other.

    spec: spec/feature/BACKEND.md §Event Catalogue (L1602-1628, table L1617-1628).
    """
    actual = set(_full_event_type_constants().values())
    missing_from_module = _EXPECTED_CATALOGUE_EVENT_TYPES - actual
    extra_in_module = actual - _EXPECTED_CATALOGUE_EVENT_TYPES
    assert not missing_from_module, (
        f"spec/feature/BACKEND.md §Event Catalogue lists these event types with no "
        f"matching constant in src/shared/events.py: {sorted(missing_from_module)}"
    )
    assert not extra_in_module, (
        f"src/shared/events.py defines these event types with no matching row in "
        f"spec/feature/BACKEND.md §Event Catalogue: {sorted(extra_in_module)} "
        f"— re-sync the spec table or remove the constant"
    )


# ── Prefix consistency ───────────────────────────────────────────────────────


def test_every_prefix_constant_prefixes_its_own_domains_event_constants() -> None:
    """Every `*_PREFIX` value is a literal prefix (including the trailing delimiter
    dot) of every one of its domain's full event strings.

    This is the invariant the domain-level `/event` endpoints rely on when they
    filter with `Event.event_type.startswith(<DOMAIN>_PREFIX)`; the trailing dot is
    what prevents a hypothetical `NODE` prefix from also matching a `NODEX` domain.

    spec: spec/feature/BACKEND_SCHEMA.md §events, L537 — domain-level endpoints
    "filter by `event_type` prefix".
    """
    prefixes = _prefix_constants()
    assert prefixes, "expected at least one *_PREFIX constant to exist"

    for domain, prefix_value in prefixes.items():
        assert prefix_value.endswith("."), (
            f"{domain}_PREFIX={prefix_value!r} does not end with '.' — a missing "
            f"delimiter would let this prefix wrongly match a differently-named "
            f"domain that shares a leading substring"
        )
        domain_constants = _domain_event_constants(domain)
        assert domain_constants, f"domain {domain!r} has a _PREFIX constant but no event constants"
        for const_name, const_value in domain_constants.items():
            assert const_value.startswith(prefix_value), (
                f"{const_name}={const_value!r} does not start with "
                f"{domain}_PREFIX={prefix_value!r}"
            )


# ── Uniqueness ────────────────────────────────────────────────────────────────


def test_no_two_event_type_constants_share_the_same_string() -> None:
    """No two distinct constant names collide on the same `"DOMAIN.ACTION"` event-type string.

    `event_type` is looked up and filtered as a single string column
    (spec/feature/BACKEND_SCHEMA.md §events, L513); a collision between two
    constants would make one domain's events indistinguishable from another's in
    the shared `events` table.
    """
    seen: dict[str, str] = {}
    for name, value in _full_event_type_constants().items():
        assert value not in seen, (
            f"{name}={value!r} collides with {seen.get(value)}={value!r}"
        )
        seen[value] = name


# ── Naming convention ─────────────────────────────────────────────────────────


def test_all_event_type_constants_follow_uppercase_dot_delimited_convention() -> None:
    """Every full event-type constant matches `UPPERCASE.DOT.DELIMITED` (`DOMAIN.ACTION`,
    each segment upper-snake-case).

    spec: spec/feature/BACKEND.md:1591 — "Event type values are **uppercase**,
    dot-delimited: `{DOMAIN}.{ACTION}`."; spec/feature/BACKEND_SCHEMA.md §events,
    `event_type` column (L513) — same rule restated at the schema layer.
    """
    full_event_constants = _full_event_type_constants()
    assert full_event_constants, "expected at least one full event-type constant to exist"

    for name, value in full_event_constants.items():
        assert _UPPERCASE_DOT_DELIMITED.match(value), (
            f"{name}={value!r} does not match the UPPERCASE.DOT.DELIMITED convention"
        )


def test_every_domain_prefix_is_the_domain_segment_of_its_events() -> None:
    """`{DOMAIN}_PREFIX` equals `"{DOMAIN}."` — the domain segment before the dot in
    every one of that domain's own event-type constants.

    spec: spec/feature/BACKEND.md:1591 — the `{DOMAIN}.{ACTION}` naming convention,
    of which `{DOMAIN}.` is the leading segment `*_PREFIX` constants exist to name.
    """
    for domain, prefix_value in _prefix_constants().items():
        assert prefix_value == f"{domain}.", (
            f"{domain}_PREFIX={prefix_value!r} does not equal the expected {domain}.'"
        )


# ── Name-to-value correspondence ────────────────────────────────────────────


def test_constant_name_matches_its_own_value_not_a_swapped_sibling() -> None:
    """Each full event-type constant's name maps to its own value by the documented
    `{DOMAIN}.{ACTION}` convention: replacing the domain/action separator underscore
    in the constant's own name with a dot must reproduce the constant's value exactly.

    Catalogue-set equality (`test_module_event_types_match_the_spec_catalogue_exactly`)
    and uniqueness (`test_no_two_event_type_constants_share_the_same_string`) both treat
    the module's constants as an unordered bag of strings, so swapping the values of two
    same-domain constants (e.g. accidentally writing `NODE_APPROVE = "NODE.REJECT"` and
    `NODE_REJECT = "NODE.APPROVE"`, or the same swap between
    `METAGEN_CONFIG_CREATE`/`METAGEN_CONFIG_UPDATE`) leaves the bag, the uppercase/dot
    regex, and every `*_PREFIX` assertion unchanged — yet every event booked under that
    constant's name would land under the wrong `event_type` string. Binding each name to
    its own value, not just to the set of all values, is what a bag-of-strings comparison
    structurally cannot catch.

    spec: spec/feature/BACKEND.md:1591 — "Event type values are **uppercase**,
    dot-delimited: `{DOMAIN}.{ACTION}`."
    """
    full_event_constants = _full_event_type_constants()
    assert full_event_constants, "expected at least one full event-type constant to exist"

    for name, value in full_event_constants.items():
        expected_value = name.replace("_", ".", 1)
        assert value == expected_value, (
            f"{name}={value!r} does not match the value its own name implies "
            f"({expected_value!r}) under the {{DOMAIN}}.{{ACTION}} convention — this "
            f"constant may have swapped values with a same-domain sibling"
        )


# ── AUTH_PREFIX absence (regression guard) ────────────────────────────────────


def test_auth_prefix_constant_does_not_exist() -> None:
    """`AUTH_PREFIX` was removed as dead code (issue #189 Finding 5): grep across
    `src/` and `tests/` found zero call sites, unlike every other domain's
    `*_PREFIX` constant, each consumed by a live
    `Event.event_type.startswith(<DOMAIN>_PREFIX)` filter:
    `src/api/routers/spoke/metagen.py` (L232, L372),
    `src/api/routers/spoke/common/data/metagen.py` (L211),
    `src/api/routers/spoke/ontogen.py` (L455-461),
    `src/backend/ontogen/service.py` (L1056-1165, for NODE/EDGE/TRIPLE),
    `src/backend/validation/service.py` (L311, L714),
    `src/backend/ingestion/service.py` (L1691), and
    `src/backend/metrics/service.py` (L715). `AUTH` has no such endpoint —
    its four events are looked up directly by `entity_id`/`event_type` equality
    (`src/api/routers/internal/activities.py`, `src/api/routers/admin.py`).

    This absence is a code-level dead-code regression guard, not an assertion
    with spec text behind it: spec/feature/BACKEND.md §Event Catalogue
    (L1625-1628) lists the four `AUTH` rows' domain/action/trigger/detail-keys but
    says nothing about prefix constants or lookup style — the "no domain-level
    prefix endpoint for AUTH" fact is inferred from the absence of an `AUTH`
    `/event` route in the routers listed above, not read out of that table.
    There are eight other domains with a live `*_PREFIX` `startswith` call site:
    `INGESTION`/`VALIDATION`/`METAGEN`/`METRIC`/`ONTOGEN`/`NODE`/`EDGE`/`TRIPLE`.
    """
    assert not hasattr(events_module, "AUTH_PREFIX")
    assert "AUTH" not in _prefix_constants()
