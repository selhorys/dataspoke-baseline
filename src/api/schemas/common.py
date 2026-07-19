import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_serializer

# Characters barred from a display URL anywhere: whitespace, C0 controls (CR/LF
# header splitting), and the unicode bidi-override set, which can visually
# disguise a hostname.
#
# U+FEFF is listed explicitly because Python's ``\s`` follows the Unicode
# White_Space property, which excludes it, while ECMAScript's ``\s`` includes it
# (``WhiteSpace`` admits ZWNBSP).  The frontend guard in
# ``src/frontend/lib/safe-url.ts`` mirrors this class and bars U+0085 explicitly
# for the converse reason.  Both sides must bar both characters or the two
# engines disagree on the same value; ``tests/fixtures/safe-url-cases.json``
# pins the agreement.
_URL_BARRED_CHARS = "\\s\\x00-\\x1f\\ufeff‎‏‪-‮⁦-⁩"

# Full-match pattern for operator-supplied URLs that clients interpolate into a
# browser ``href``.  Admits only ``http``/``https`` (so ``javascript:``,
# ``data:``, and ``vbscript:`` cannot reach an anchor) or ``""`` meaning unset.
# Userinfo is rejected outright — the authority admits a host plus an optional
# numeric port and nothing else — so a credential-shaped prefix cannot disguise
# the effective host (``https://trusted.example.com@evil.com``).
SAFE_DISPLAY_URL_PATTERN = (
    f"^$|^https?://[^{_URL_BARRED_CHARS}:/?#@]+(?::[0-9]+)?(?:/[^{_URL_BARRED_CHARS}]*)?$"
)

SAFE_DISPLAY_URL_MAX_LENGTH = 512

# Langfuse project ids are opaque slugs; constrain them because they are
# interpolated into a deep-link path segment.
SAFE_PROJECT_ID_PATTERN = r"^$|^[A-Za-z0-9][A-Za-z0-9_-]*$"

SAFE_PROJECT_ID_MAX_LENGTH = 256

# ``re.fullmatch`` rather than ``re.match``: Python's ``$`` also matches just
# before a trailing newline, which would admit a value the pydantic write
# boundary (rust-regex, end-of-haystack ``$``) rejects.  fullmatch keeps the
# read-side check exactly as strict as the write-side one.
_SAFE_DISPLAY_URL_RE = re.compile(SAFE_DISPLAY_URL_PATTERN)
_SAFE_PROJECT_ID_RE = re.compile(SAFE_PROJECT_ID_PATTERN)


def sanitize_display_url(value: str | None) -> str:
    """Return *value* when it is a safe display URL, else ``""``.

    ``peripheral_config.settings`` is untyped JSONB written through a kwargs
    sink, so a row seeded by direct SQL or by a caller that bypasses the admin
    request schema can hold anything.  Values reaching a browser ``href`` are
    therefore re-checked on read and degraded to ``""`` — which clients already
    treat as "render no link" — rather than forwarded verbatim.
    """
    if not value:
        return ""
    if len(value) > SAFE_DISPLAY_URL_MAX_LENGTH:
        return ""
    return value if _SAFE_DISPLAY_URL_RE.fullmatch(value) else ""


def sanitize_project_id(value: str | None) -> str:
    """Return *value* when it is a safe project-id slug, else ``""``."""
    if not value:
        return ""
    if len(value) > SAFE_PROJECT_ID_MAX_LENGTH:
        return ""
    return value if _SAFE_PROJECT_ID_RE.fullmatch(value) else ""


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _serialize_resp_time(value: datetime) -> str:
    """Render ``resp_time`` as ISO-8601 UTC with exactly millisecond precision.

    Matches the error-path format in ``src/api/main.py`` (``2026-02-27T10:00:00.000Z``).
    Pydantic v2's default datetime serializer emits microsecond precision and drops
    the fractional part entirely when zero, so success envelopes need this override
    to stay byte-for-byte consistent with error responses (API.md §Date/Time).
    """
    dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S.')}{dt.microsecond // 1000:03d}Z"


_RESP_TIME_DESC = "UTC timestamp when this response was generated"


class ErrorResponse(BaseModel):
    error_code: str = Field(
        description="Machine-readable error code, e.g. 'NOT_FOUND' or 'VALIDATION_ERROR'"
    )
    message: str = Field(description="Human-readable description of the error")
    trace_id: str = Field(description="Unique request trace identifier for log correlation")
    resp_time: datetime = Field(default_factory=_now_utc, description=_RESP_TIME_DESC)

    @field_serializer("resp_time")
    def _ser_resp_time(self, value: datetime) -> str:
        return _serialize_resp_time(value)


class PaginatedResponse(BaseModel):
    offset: int = Field(default=0, description="Number of items skipped before this page")
    limit: int = Field(default=20, description="Maximum number of items returned in this page")
    total_count: int = Field(
        default=0, description="Total number of items matching the query across all pages"
    )
    resp_time: datetime = Field(default_factory=_now_utc, description=_RESP_TIME_DESC)
    # Subclasses add a typed list field named after the resource.
    # This base class carries only the pagination envelope.

    @field_serializer("resp_time")
    def _ser_resp_time(self, value: datetime) -> str:
        return _serialize_resp_time(value)


class SingleResponse(BaseModel):
    resp_time: datetime = Field(default_factory=_now_utc, description=_RESP_TIME_DESC)

    @field_serializer("resp_time")
    def _ser_resp_time(self, value: datetime) -> str:
        return _serialize_resp_time(value)


class PaginationParams(BaseModel):
    offset: int = Field(default=0, ge=0, description="Number of items to skip (0-based)")
    limit: int = Field(
        default=20, ge=1, le=1000, description="Maximum number of items to return (1–1000)"
    )
    sort: str | None = Field(
        default=None,
        description=(
            "Sort expression in the form '<field>_asc' or '<field>_desc', e.g. 'created_at_desc'"
        ),
    )


class TimeRangeParams(BaseModel):
    from_time: datetime | None = Field(
        default=None, alias="from", description="Inclusive start of the time window (ISO-8601 UTC)"
    )
    to_time: datetime | None = Field(
        default=None, alias="to", description="Inclusive end of the time window (ISO-8601 UTC)"
    )

    model_config = {"populate_by_name": True}


def parse_sort(sort_param: str | None, allowed: dict[str, Any], default: Any) -> Any:
    """Parse 'field_asc'/'field_desc' into SQLAlchemy order clause.

    Args:
        sort_param: User-supplied sort string, e.g. ``"created_at_desc"``
        allowed: Mapping of field name -> SQLAlchemy column
        default: Fallback order clause when sort_param is None or invalid
    """
    if sort_param is None:
        return default
    for suffix, method in (("_desc", "desc"), ("_asc", "asc")):
        if sort_param.endswith(suffix):
            field_name = sort_param[: -len(suffix)]
            col = allowed.get(field_name)
            if col is not None:
                return getattr(col, method)()
            break
    return default
