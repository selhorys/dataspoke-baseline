from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
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
    assert EntityNotFoundError("concept", "c1").error_code == "CONCEPT_NOT_FOUND"
    assert EntityNotFoundError("config", "cfg").error_code == "CONFIG_NOT_FOUND"


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
