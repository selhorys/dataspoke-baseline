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
