"""DataSpoke shared exceptions.

All backend services raise subclasses of DataSpokeError.
The API layer catches these and maps them to HTTP responses.

Convention: entity_type strings passed to EntityNotFoundError must be lowercase
singular nouns (e.g. "dataset", "config", "metric", "concept") — the error_code
is derived as entity_type.upper() + "_NOT_FOUND".
"""


class DataSpokeError(Exception):
    """Base exception for all DataSpoke backend errors."""

    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class EntityNotFoundError(DataSpokeError):
    """Raised when a requested entity does not exist."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.error_code = f"{entity_type.upper()}_NOT_FOUND"
        super().__init__(f"{entity_type} '{entity_id}' not found")


class ConflictError(DataSpokeError):
    """Raised when an operation conflicts with current state."""

    def __init__(self, error_code: str, message: str = "") -> None:
        self.error_code = error_code
        super().__init__(message)


class DataHubUnavailableError(DataSpokeError):
    """Raised when DataHub GMS is unreachable or returns an error."""

    error_code: str = "DATAHUB_UNAVAILABLE"


class StorageUnavailableError(DataSpokeError):
    """Raised when PostgreSQL, Redis, or Qdrant is unreachable."""

    error_code: str = "STORAGE_UNAVAILABLE"


class NotificationError(DataSpokeError):
    """Raised when a notification (e.g. email) fails to send."""

    error_code: str = "NOTIFICATION_FAILED"


class EventProcessingError(DataSpokeError):
    """Raised when a Kafka event handler fails to process an event."""

    error_code: str = "EVENT_PROCESSING_FAILED"
