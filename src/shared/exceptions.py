"""DataSpoke shared exceptions.

All backend services raise subclasses of DataSpokeError.
The API layer catches these and maps them to HTTP responses.

Exception-to-HTTP mapping (per spec/feature/BACKEND.md §Error Handling):

  EntityNotFoundError   → 404  DATASET_NOT_FOUND | CONFIG_NOT_FOUND | METRIC_NOT_FOUND
                                 | NODE_NOT_FOUND | EDGE_NOT_FOUND | TRIPLE_NOT_FOUND
  ConflictError         → 409  DUPLICATE_CONFIG | INGESTION_RUNNING
                                 | GENERATION_RUNNING | METRIC_RUNNING | ONTOGEN_RUNNING
                                 | INGESTION_DISABLED | INGESTION_NOT_APPLICABLE
                                 | GENERATION_DISABLED | METRIC_DISABLED | ONTOGEN_DISABLED
  DataHubUnavailableError → 502  DATAHUB_UNAVAILABLE
  StorageUnavailableError → 503  STORAGE_UNAVAILABLE
  ValidationError (Pydantic) → 422  INVALID_PARAMETER | INVALID_DATASET_URN
  PreconditionFailedError → 422  DATASET_NOT_IN_DATAHUB | ONTOGEN_TRIPLE_DEPENDENCY_PENDING
                                   | UNKNOWN_VARIABLE | INVALID_SCORE

Convention: entity_type strings passed to EntityNotFoundError must be lowercase
singular nouns (e.g. "dataset", "config", "metric", "node", "edge", "triple") —
the error_code is derived as entity_type.upper() + "_NOT_FOUND".
"""

import re

# Strip ASCII control characters (0x00–0x1f, 0x7f) from user-supplied values
# before embedding them in exception messages to prevent log injection.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class DataSpokeError(Exception):
    """Base exception for all DataSpoke backend errors."""

    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class EntityNotFoundError(DataSpokeError):
    """Raised when a requested entity does not exist.

    Valid entity_type values (each maps to a 404 error code):
      "dataset"  → DATASET_NOT_FOUND
      "config"   → CONFIG_NOT_FOUND
      "metric"   → METRIC_NOT_FOUND
      "node"     → NODE_NOT_FOUND
      "edge"     → EDGE_NOT_FOUND
      "triple"   → TRIPLE_NOT_FOUND
    """

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.error_code = f"{entity_type.upper()}_NOT_FOUND"
        _safe_id = _CTRL_RE.sub("?", str(entity_id))
        super().__init__(f"{entity_type} '{_safe_id}' not found")


class ConflictError(DataSpokeError):
    """Raised when an operation conflicts with current state (HTTP 409).

    Valid error_code values:
      DUPLICATE_CONFIG              — attempt to create a config that already exists
      INGESTION_RUNNING             — concurrent active ingestion run for the dataset
      GENERATION_RUNNING            — concurrent metadata-generation run for the dataset
      METRIC_RUNNING                — concurrent metric measurement
      METRIC_EXISTS                 — POST /spoke/dg/metric carries a metric_id that already exists
      ONTOGEN_RUNNING               — ontogen singleton inference already in progress
      METAGEN_RUNNING               — metagen singleton inference already in progress
      INGESTION_DISABLED            — ingestion conf has is_enabled=false; only dry-run permitted
      INGESTION_NOT_APPLICABLE      — method/run called on a passive config; run externally
      GENERATION_DISABLED           — metagen conf has is_enabled=false; only dry-run permitted
      METRIC_DISABLED               — metric definition has is_enabled=false; only dry-run permitted
      ONTOGEN_DISABLED              — ontogen conf has is_enabled=false; only dry-run permitted
      METAGEN_DISABLED              — metagen conf has is_enabled=false; only dry-run permitted
      METAGEN_CANNOT_REJECT_APPROVED — reject verdict on a candidate whose status is approved
    """

    def __init__(self, error_code: str, message: str = "") -> None:
        self.error_code = error_code
        super().__init__(message)


class PreconditionFailedError(DataSpokeError):
    """Raised when a precondition for an operation is not met (HTTP 422).

    Valid error_code values:
      DATASET_NOT_IN_DATAHUB            — dataset URN not registered in DataHub
      ONTOGEN_TRIPLE_DEPENDENCY_PENDING — triple approval attempted when endpoint
                                          nodes or edge are not yet approved
      UNKNOWN_VARIABLE                  — result POST carries variable keys not declared
                                          in the dataset's validation conf
      INVALID_SCORE                     — result POST has score outside [0.0, 1.0]
      METAGEN_DATASET_NOT_IN_BOUNDARY   — candidate review on a dataset with no
                                          is_enabled=true metagen boundary
    """

    def __init__(self, error_code: str, message: str = "", detail: dict | None = None) -> None:
        self.error_code = error_code
        self.detail = detail or {}
        super().__init__(message)


class DataHubUnavailableError(DataSpokeError):
    """Raised when DataHub GMS is unreachable or returns an error."""

    error_code: str = "DATAHUB_UNAVAILABLE"


class StorageUnavailableError(DataSpokeError):
    """Raised when PostgreSQL or Redis is unreachable."""

    error_code: str = "STORAGE_UNAVAILABLE"


class AuthenticationError(DataSpokeError):
    """Raised for auth flow failures (invalid credentials, missing/revoked refresh
    cookie, malformed JWT). Maps to HTTP 401.

    Valid error_code values:
      UNAUTHORIZED          — default; malformed or missing token
      TOKEN_REVOKED         — API token has been revoked
      TOKEN_EXPIRED         — API token has passed its expires_at
      INVALID_API_TOKEN     — API token not found or hash mismatch
      OAUTH_STATE_MISMATCH  — Google OAuth state cookie did not match
    """

    error_code: str = "UNAUTHORIZED"

    def __init__(self, message: str = "Unauthorized", error_code: str = "UNAUTHORIZED") -> None:
        self.error_code = error_code
        super().__init__(message)


class OAuthNotConfiguredError(DataSpokeError):
    """Raised when an OAuth operation is attempted but credentials are not configured.
    Maps to HTTP 503.

    error_code: OAUTH_NOT_CONFIGURED
    """

    error_code: str = "OAUTH_NOT_CONFIGURED"


class BadRequestError(DataSpokeError):
    """Raised for client-supplied inputs that are syntactically valid but semantically
    wrong (HTTP 400).

    Valid error_code values:
      BAD_REQUEST           — default
      INVALID_RESET_TOKEN   — password reset token missing, used, or expired
    """

    error_code: str = "BAD_REQUEST"

    def __init__(self, message: str = "Bad request", error_code: str = "BAD_REQUEST") -> None:
        self.error_code = error_code
        super().__init__(message)


class ForbiddenError(DataSpokeError):
    """Raised when the caller is authenticated but lacks the required privilege.
    Maps to HTTP 403.

    Valid error_code values:
      FORBIDDEN        — default; caller role not sufficient (e.g. Reader on /admin/*)
      READ_ONLY_ROLE   — Reader attempting a write method on /spoke/* or /hub/*
    """

    error_code: str = "FORBIDDEN"

    def __init__(self, message: str = "Forbidden", error_code: str = "FORBIDDEN") -> None:
        self.error_code = error_code
        super().__init__(message)


class PeripheralNotConfiguredError(DataSpokeError):
    """Raised when an operation requires a peripheral (e.g. smtp) that is not
    configured. Maps to HTTP 503.

    error_code: PERIPHERAL_NOT_CONFIGURED
    detail: {"peripheral": "<name>"}
    """

    error_code: str = "PERIPHERAL_NOT_CONFIGURED"

    def __init__(self, peripheral: str) -> None:
        self.detail: dict[str, object] = {"peripheral": peripheral}
        super().__init__(f"Peripheral '{peripheral}' is not configured")


class DataHubSyncError(DataSpokeError):
    """Raised when the DataHub mirror write fails during user registration and the
    compensating local-row delete is triggered. Maps to HTTP 503.

    Distinct from DataHubUnavailableError because this code is specific to the
    register-time compensating-delete path, allowing callers to distinguish a
    transient transport failure (502) from a sync failure that left the system
    in a clean state after rollback (503).
    """

    error_code: str = "DATAHUB_SYNC_FAILED"


class NotificationError(DataSpokeError):
    """Raised when a notification (e.g. email) fails to send."""

    error_code: str = "NOTIFICATION_FAILED"


class EventProcessingError(DataSpokeError):
    """Raised when a Kafka event handler fails to process an event."""

    error_code: str = "EVENT_PROCESSING_FAILED"


class InvalidDatasetUrnError(DataSpokeError):
    """Raised when a dataset URN fails format validation (HTTP 422).

    Convenience subclass so callers don't need to construct ConflictError
    or PreconditionFailedError by hand for this common case.
    """

    error_code: str = "INVALID_DATASET_URN"

    def __init__(self, urn: str, message: str = "") -> None:
        _safe_urn = _CTRL_RE.sub("?", str(urn))
        super().__init__(message or f"Invalid dataset URN: {_safe_urn!r}")


class NotImplementedAPIError(DataSpokeError):
    """Raised when the requested mode or capability is reserved for future work.

    Maps to HTTP 501 NOT_IMPLEMENTED. Used for features that are defined in the
    API schema but not yet operational (e.g. passive metric mode).
    """

    error_code: str = "NOT_IMPLEMENTED"

    def __init__(self, message: str = "Not implemented") -> None:
        super().__init__(message)
