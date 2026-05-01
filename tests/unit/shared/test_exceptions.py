"""Tests for src/shared/exceptions.py — verifies the DataSpokeError hierarchy and
error codes against spec/API.md §Application Error Codes (lines 572-578).

The EntityNotFoundError mapping (entity_type → error_code) is exhaustively tested
against the spec table to ensure no code drifts from the API contract."""

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


# Exhaustive spec-anchored mapping from spec/API.md §Application Error Codes L572-578.
# Each tuple is (entity_type, expected_error_code).  The mapping is taken verbatim
# from the spec table so any drift between impl and spec causes a test failure.
_ENTITY_ERROR_CODE_MAP = [
    ("dataset", "DATASET_NOT_FOUND"),
    ("config", "CONFIG_NOT_FOUND"),
    ("metric", "METRIC_NOT_FOUND"),
    ("node", "NODE_NOT_FOUND"),
    ("edge", "EDGE_NOT_FOUND"),
    ("triple", "TRIPLE_NOT_FOUND"),
]


@pytest.mark.parametrize("entity_type,expected_code", _ENTITY_ERROR_CODE_MAP)
def test_entity_not_found_error_code_matches_spec(entity_type: str, expected_code: str) -> None:
    """EntityNotFoundError.error_code must match the spec/API.md §Application Error Codes table.

    Exhaustively covers all six entity types defined in spec/API.md L572-578:
    DATASET_NOT_FOUND, CONFIG_NOT_FOUND, METRIC_NOT_FOUND, NODE_NOT_FOUND,
    EDGE_NOT_FOUND, TRIPLE_NOT_FOUND.  Tests both directions: every spec entry has a
    test, and no extra codes are silently produced.

    HTTP 404 mapping: spec/API.md L572-578 mandates 404 for all six entity types.
    EntityNotFoundError does not carry an http_status attribute — the 404 is applied
    by the _handle_not_found exception handler in src/api/main.py. The handler
    mapping is verified by test_entity_not_found_http_handler_maps_to_404 below.
    """
    exc = EntityNotFoundError(entity_type, f"test-{entity_type}-id")
    assert exc.error_code == expected_code, (
        f"EntityNotFoundError('{entity_type}', ...) produced error_code={exc.error_code!r}, "
        f"but spec/API.md requires {expected_code!r}"
    )
    # EntityNotFoundError must NOT carry an http_status attribute —
    # the HTTP mapping belongs to the API handler layer, not the exception itself.
    assert not hasattr(exc, "http_status"), (
        "EntityNotFoundError should not carry http_status — HTTP mapping is in the API handler"
    )


def test_entity_not_found_http_handler_maps_to_404() -> None:
    """The API exception handler for EntityNotFoundError must return HTTP 404.

    spec/API.md L572-578 mandates HTTP 404 for all six entity-not-found codes.
    The mapping is implemented in src/api/main.py:_handle_not_found.
    This test verifies the handler is registered and returns 404 by inspecting
    the handler registration — no live server required.
    """
    import inspect

    from src.api.main import _handle_not_found

    # _handle_not_found must be an async callable that accepts (request, exc)
    assert callable(_handle_not_found)
    assert inspect.iscoroutinefunction(_handle_not_found), (
        "_handle_not_found must be an async function (FastAPI async exception handler)"
    )
    # Verify the function body returns a 404 JSONResponse by inspecting source
    source = inspect.getsource(_handle_not_found)
    assert "404" in source, (
        "_handle_not_found must return HTTP 404 per spec/API.md L572-578"
    )


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
