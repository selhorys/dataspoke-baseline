"""Programmatic response-format compliance checks.

Ensures Pydantic schemas follow API.md conventions:
- All response fields are snake_case
- All collection responses include pagination envelope
- All single-item responses include resp_time
- Error response has required fields
- No remaining 501 stubs in routers
"""

import importlib
import pkgutil
import re

import pytest
from pydantic import BaseModel

from src.api.schemas.common import ErrorResponse, PaginatedResponse, SingleResponse

# ── Schema discovery ──────────────────────────────────────────────────────────

_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _discover_response_models() -> list[type[BaseModel]]:
    """Import all schema modules and collect *Response classes."""
    import src.api.schemas as schemas_pkg

    models: list[type[BaseModel]] = []
    for importer, mod_name, _ in pkgutil.walk_packages(
        schemas_pkg.__path__, prefix=schemas_pkg.__name__ + "."
    ):
        mod = importlib.import_module(mod_name)
        for attr_name in dir(mod):
            if not attr_name.endswith("Response"):
                continue
            cls = getattr(mod, attr_name)
            if isinstance(cls, type) and issubclass(cls, BaseModel) and cls is not BaseModel:
                models.append(cls)
    return list(set(models))


_ALL_RESPONSE_MODELS = _discover_response_models()

# Separate into list (paginated) and single-item responses
_LIST_RESPONSES = [m for m in _ALL_RESPONSE_MODELS if issubclass(m, PaginatedResponse)]
_SINGLE_RESPONSES = [
    m
    for m in _ALL_RESPONSE_MODELS
    if issubclass(m, SingleResponse)
    and not issubclass(m, PaginatedResponse)
    and m is not SingleResponse
]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_all_list_responses_have_pagination_fields() -> None:
    """Every *ListResponse must have offset, limit, total_count, resp_time."""
    required = {"offset", "limit", "total_count", "resp_time"}
    for model in _LIST_RESPONSES:
        fields = set(model.model_fields.keys())
        missing = required - fields
        assert not missing, f"{model.__name__} missing pagination fields: {missing}"


def test_all_single_responses_have_resp_time() -> None:
    """Every single-item *Response must have resp_time."""
    for model in _SINGLE_RESPONSES:
        assert "resp_time" in model.model_fields, f"{model.__name__} missing resp_time"


def test_all_response_fields_are_snake_case() -> None:
    """No camelCase field names in any response model."""
    violations: list[str] = []
    for model in _ALL_RESPONSE_MODELS:
        for field_name in model.model_fields:
            if _CAMEL_RE.search(field_name):
                violations.append(f"{model.__name__}.{field_name}")
    assert not violations, f"camelCase fields found: {violations}"


def test_error_response_has_required_fields() -> None:
    """ErrorResponse must have error_code, message, trace_id."""
    fields = set(ErrorResponse.model_fields.keys())
    assert {"error_code", "message", "trace_id"} <= fields


def test_discovered_models_are_not_empty() -> None:
    """Sanity check: ensure discovery finds a reasonable number of models."""
    assert len(_ALL_RESPONSE_MODELS) >= 15, (
        f"Expected at least 15 response models, found {len(_ALL_RESPONSE_MODELS)}"
    )


@pytest.mark.asyncio
async def test_no_remaining_501_stubs() -> None:
    """Verify no router handler raises 501 NOT_IMPLEMENTED."""
    import src.api.routers as routers_pkg

    for importer, mod_name, _ in pkgutil.walk_packages(
        routers_pkg.__path__, prefix=routers_pkg.__name__ + "."
    ):
        mod = importlib.import_module(mod_name)

    # After importing all routers, check the app's routes
    from src.api.main import app

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        # Check if the function source contains "raise _501" or "501"
        import inspect

        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        assert "HTTP_501_NOT_IMPLEMENTED" not in source, (
            f"Route endpoint {endpoint.__name__} still contains a 501 stub"
        )
        assert "raise _501" not in source, f"Route endpoint {endpoint.__name__} still raises _501"
