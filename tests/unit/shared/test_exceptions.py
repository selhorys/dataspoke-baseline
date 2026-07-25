"""Tests for src/shared/exceptions.py — verifies the DataSpokeError hierarchy and
error codes against spec/API.md §Application Error Codes.

The EntityNotFoundError mapping (entity_type → error_code) is parsed from the
declarative block in EntityNotFoundError's own docstring rather than hand-listed here,
so the two cannot drift apart. That the parsed codes are catalogued in spec/API.md — and
catalogued as 404 — is asserted in tests/unit/spec_conformance/test_error_catalogue.py::
TestExceptionDeclarationsAreCatalogued::test_entity_not_found_codes_are_404_in_api_md."""

import pytest

from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
    StorageUnavailableError,
)
from tests.unit.spec_conformance._api_md import entity_not_found_map


def test_dataspokerror_default_error_code() -> None:
    exc = DataSpokeError()
    assert exc.error_code == "INTERNAL_ERROR"


def test_dataspokerror_with_message() -> None:
    exc = DataSpokeError("something went wrong")
    assert str(exc) == "something went wrong"


def test_entity_not_found_derives_code() -> None:
    exc = EntityNotFoundError("dataset", "urn:li:dataset:123")
    assert exc.error_code == "DATASET_NOT_FOUND"


def test_entity_not_found_message() -> None:
    exc = EntityNotFoundError("dataset", "urn:li:dataset:123")
    msg = str(exc)
    assert "dataset" in msg
    assert "urn:li:dataset:123" in msg


def test_entity_not_found_is_dataspokerror() -> None:
    exc = EntityNotFoundError("config", "cfg-1")
    assert isinstance(exc, DataSpokeError)


def test_entity_not_found_various_types() -> None:
    assert EntityNotFoundError("metric", "m1").error_code == "METRIC_NOT_FOUND"
    assert EntityNotFoundError("node", "n1").error_code == "NODE_NOT_FOUND"
    assert EntityNotFoundError("edge", "e1").error_code == "EDGE_NOT_FOUND"
    assert EntityNotFoundError("triple", "t1").error_code == "TRIPLE_NOT_FOUND"
    assert EntityNotFoundError("config", "cfg").error_code == "CONFIG_NOT_FOUND"


# Derived, never hand-listed: the (entity_type, expected_error_code) pairs are parsed
# from the "Valid entity_type values" block of EntityNotFoundError's own docstring —
# the only declarative enumeration of which entity types are valid. A hand-maintained
# copy drifts silently the moment a type is added.
# That every derived code is catalogued in spec/API.md §Application Error Codes with
# HTTP 404 is asserted separately, in tests/unit/spec_conformance/test_error_catalogue.py
# ::TestExceptionDeclarationsAreCatalogued::test_entity_not_found_codes_are_404_in_api_md.
_ENTITY_ERROR_CODE_MAP = sorted(entity_not_found_map().items())

# Backstop: the parametrization above is generated, so a degraded parse would turn every
# derived case into a silent no-op — and a *partial* degradation is the dangerous one.
# Narrowing the parser's entity-type pattern to a single word, for instance, drops every
# multi-segment type (`ingestion_source`, `metagen_boundary`, `dag_group`, …). Equality
# (not containment) is therefore asserted, so shrinking OR widening the parsed set fails
# here rather than quietly resizing the parametrization.
#
# Every entry is codified elsewhere too: each maps to a code that spec/API.md
# §Application Error Codes lists at 404 (e.g. DATASET_NOT_FOUND, SEED_NOT_FOUND,
# USER_NOT_FOUND), which tests/unit/spec_conformance/test_error_catalogue.py asserts
# separately — as it does that each declared type is passed at a real call site under
# src/ and that no call site passes an undeclared one.
_EXPECTED_ENTITY_TYPES = frozenset(
    {
        "config",
        "dag_group",
        "dataset",
        "edge",
        "ingestion_source",
        "metagen_boundary",
        "metagen_candidate",
        "metagen_conf",
        "metagen_item",
        "metric",
        "node",
        "seed",
        "token",
        "triple",
        "user",
    }
)


def test_entity_error_code_map_is_parsed() -> None:
    """The derived entity_type map must match the declared set exactly."""
    parsed = dict(_ENTITY_ERROR_CODE_MAP)
    assert set(parsed) == _EXPECTED_ENTITY_TYPES, (
        f"entity types parsed from EntityNotFoundError's docstring were "
        f"{sorted(parsed)}, expected {sorted(_EXPECTED_ENTITY_TYPES)}. Missing entries "
        f"mean the parser degraded and the generated parametrization below is running "
        f"on an incomplete list; extra entries mean the docstring gained a type that "
        f"must also be catalogued in spec/API.md and added here."
    )


@pytest.mark.parametrize("entity_type,expected_code", _ENTITY_ERROR_CODE_MAP)
def test_entity_not_found_error_code_matches_spec(entity_type: str, expected_code: str) -> None:
    """EntityNotFoundError.error_code must match its declared entity_type mapping.

    Covers every entity_type declared in src/shared/exceptions.py::EntityNotFoundError,
    whose codes are cross-checked against spec/API.md §Application Error Codes by the
    spec_conformance suite.

    HTTP 404 mapping: spec/API.md §Application Error Codes assigns 404 to every entity
    code. EntityNotFoundError does not carry an http_status attribute — the 404 is
    applied by the _handle_not_found exception handler in src/api/main.py. The handler
    mapping is verified by test_entity_not_found_http_handler_maps_to_404 below.
    """
    exc = EntityNotFoundError(entity_type, f"test-{entity_type}-id")
    assert exc.error_code == expected_code, (
        f"EntityNotFoundError('{entity_type}', ...) produced error_code={exc.error_code!r}, "
        f"but its own docstring declares {expected_code!r}"
    )
    # EntityNotFoundError must NOT carry an http_status attribute —
    # the HTTP mapping belongs to the API handler layer, not the exception itself.
    assert not hasattr(exc, "http_status"), (
        "EntityNotFoundError should not carry http_status — HTTP mapping is in the API handler"
    )


async def test_entity_not_found_http_handler_maps_to_404() -> None:
    """The API exception handler for EntityNotFoundError must return HTTP 404.

    spec/API.md §Application Error Codes assigns HTTP 404 to every entity-not-found
    code, and §Error Catalogue fixes the envelope shape ({error_code, message,
    trace_id, resp_time}). The mapping is implemented in src/api/main.py:_handle_not_found.

    The handler is *invoked* rather than source-inspected: a handler carrying "404" in a
    comment, or returning 404 on only one branch, would satisfy a substring check while
    returning the wrong status. Building a Request from a bare ASGI scope keeps this
    unit-tier — no server, no transport.
    """
    import json

    from starlette.requests import Request

    from src.api.main import _handle_not_found

    request = Request({"type": "http", "method": "GET", "path": "/probe", "headers": []})
    response = await _handle_not_found(
        request, EntityNotFoundError("dataset", "urn:li:dataset:probe")
    )

    assert response.status_code == 404, (
        f"_handle_not_found returned {response.status_code}; spec/API.md §Application "
        f"Error Codes requires 404 for entity-not-found codes"
    )
    body = json.loads(bytes(response.body))
    assert body["error_code"] == "DATASET_NOT_FOUND"
    assert "urn:li:dataset:probe" in body["message"]
    # spec/API.md §Error Catalogue: every error response carries trace_id and resp_time.
    assert "trace_id" in body
    assert "resp_time" in body


def test_precondition_failed_error_code() -> None:
    exc = PreconditionFailedError("ONTOGEN_TRIPLE_DEPENDENCY_PENDING")
    assert exc.error_code == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"
    assert isinstance(exc, DataSpokeError)


def test_precondition_failed_dataset_not_in_datahub() -> None:
    exc = PreconditionFailedError("DATASET_NOT_IN_DATAHUB", "dataset not registered")
    assert exc.error_code == "DATASET_NOT_IN_DATAHUB"
    assert str(exc) == "dataset not registered"


def test_invalid_dataset_urn_error() -> None:
    exc = InvalidDatasetUrnError("bad-urn")
    assert exc.error_code == "INVALID_DATASET_URN"
    assert isinstance(exc, DataSpokeError)
    assert "bad-urn" in str(exc)


def test_conflict_error_code() -> None:
    exc = ConflictError("INGESTION_RUNNING")
    assert exc.error_code == "INGESTION_RUNNING"


def test_conflict_error_with_message() -> None:
    exc = ConflictError("DUPLICATE_CONFIG", "config already exists")
    assert exc.error_code == "DUPLICATE_CONFIG"
    assert str(exc) == "config already exists"


def test_datahub_unavailable_error_code() -> None:
    exc = DataHubUnavailableError()
    assert exc.error_code == "DATAHUB_UNAVAILABLE"


def test_storage_unavailable_error_code() -> None:
    exc = StorageUnavailableError()
    assert exc.error_code == "STORAGE_UNAVAILABLE"


def test_all_inherit_dataspokerror() -> None:
    for exc in [
        EntityNotFoundError("dataset", "x"),
        ConflictError("INGESTION_RUNNING"),
        DataHubUnavailableError(),
        StorageUnavailableError(),
    ]:
        assert isinstance(exc, DataSpokeError)
        assert isinstance(exc, Exception)
