"""Stub service implementations for test mode.

When ``DATASPOKE_TEST_MODE=true``, the ``make_*`` factories in
``_common.py`` return these stubs instead of real clients.  This lets the
API process run integration scenarios without requiring real LLM /
pgvector / cache / notification backends.

DataHub and PostgreSQL are **never** stubbed — they always use real
dev-env connections, even in test mode.

Adding a new stub
-----------------
When a new external service is introduced:

1. Create a ``Stub<Service>`` class in this module that implements the
   same async interface as the real client (only the methods called by
   activity endpoints need to be stubbed).
2. Add a ``make_<service>()`` factory in ``_common.py`` with the
   ``if settings.test_mode:`` guard that imports and returns the stub.
3. Update the stub table in ``spec/TESTING.md §Test-mode stubs``.
"""

from __future__ import annotations

import logging
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from src.shared.config import EMBEDDING_DIMENSION
from src.shared.llm.loop_trace import LoopResult, LoopTrace

logger = logging.getLogger(__name__)


# ── LLM stub ────────────────────────────────────────────────────────────────


class StubLLMClient:
    """Drop-in replacement for ``LLMClient`` that returns canned responses."""

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return "stub"

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        schema: type[BaseModel] | None = None,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        if schema is None:
            return {}
        return _minimal_dict_for_schema(schema)

    async def complete_with_tools(
        self,
        prompt: str,
        *,
        tools: list,
        success_tool_name: str,
        schema: type[BaseModel],
        system: str = "",
        max_iterations: int = 3,
        temperature: float = 0.0,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LoopResult:
        if success_tool_name == "ontogen_review":
            return LoopResult(
                payload={
                    "overall_verdict": "accept",
                    "item_verdicts": [],
                    "summary": "stub-accept",
                },
                trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
            )

        if success_tool_name == "metagen_review":
            return LoopResult(
                payload={
                    "overall_verdict": "accept",
                    "item_verdicts": [],
                    "summary": "stub-accept",
                },
                trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
            )

        if success_tool_name == "metagen_validate":
            try:
                import json as _json

                marker = "TARGET ITEMS (generate one candidate per item):\n"
                end_marker = "\n\nDATASET EVIDENCE:"
                start_idx = prompt.index(marker) + len(marker)
                end_idx = prompt.index(end_marker, start_idx)
                target_items = _json.loads(prompt[start_idx:end_idx])
                candidates = []
                for entry in target_items:
                    dataset_urn = entry["dataset_urn"]
                    item_id = entry["item_id"]
                    field_path = entry.get("field_path") or ""
                    if item_id == "dataset.description":
                        value = f"[stub] Description for {dataset_urn}"
                    else:
                        value = f"[stub] Column {field_path} description"
                    candidates.append(
                        {
                            "dataset_urn": dataset_urn,
                            "item_id": item_id,
                            "value": value,
                            "confidence_score": 0.85,
                        }
                    )
                return LoopResult(
                    payload={"candidates": candidates},
                    trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
                )
            except Exception:
                pass  # fall through to _minimal_dict_for_schema below

        return LoopResult(
            payload=_minimal_dict_for_schema(schema),
            trace=LoopTrace(iterations=1, errors_per_iter=[], final_errors=[]),
        )


def _minimal_dict_for_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Build a minimal dict that satisfies *schema*'s type annotations.

    Iterates over Pydantic model fields and generates a type-appropriate
    default for each (e.g., ``""`` for str, ``0`` for int, ``[]`` for list).
    For ``Optional[T]`` / ``Union[T, None]`` fields, produces the non-None
    default.  Nested Pydantic models are not recursed into — they get
    ``None``.
    """
    result: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        result[name] = _default_for_annotation(field.annotation)
    return result


def _default_for_annotation(annotation: Any) -> Any:  # noqa: PLR0911
    """Return a minimal default value for a Python type annotation.

    Handles ``str``, ``int``, ``float``, ``bool``, ``list``, ``dict``,
    and ``Optional``/``Union`` (picks the first non-None branch).
    Returns ``None`` for unrecognized types.
    """
    origin = get_origin(annotation)
    if origin is dict:
        return {}
    if origin is list:
        return []
    if annotation is str:
        return "stub"
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    # Union / Optional — try first non-None arg
    args = get_args(annotation)
    if args:
        for arg in args:
            if arg is not type(None):
                return _default_for_annotation(arg)
    return None


# ── pgvector stub ───────────────────────────────────────────────────────────


class StubVectorManager:
    """Drop-in replacement for ``PgVectorManager`` — searches return nothing."""

    async def ensure_collection(self, name: str = "", vector_size: int = EMBEDDING_DIMENSION) -> None:
        pass

    async def upsert(self, collection: str, hits: list) -> None:
        pass

    async def search(self, collection: str = "", vector: list | None = None, **kwargs: Any) -> list:
        return []

    async def delete(self, collection: str = "", ids: list | None = None, **kwargs: Any) -> None:
        pass

    async def check_connectivity(self) -> bool:
        return True


# ── Redis/cache stub ────────────────────────────────────────────────────────


class StubRedisClient:
    """Drop-in replacement for ``RedisClient`` — every op is a no-op."""

    async def get(self, key: str) -> Any:
        return None

    async def set(self, key: str, value: Any, **kwargs: Any) -> None:
        pass

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 300) -> bool:
        return True

    async def publish(self, channel: str, message: Any) -> None:
        pass

    async def delete(self, key: str) -> None:
        pass

    async def delete_if_value(self, key: str, expected: str) -> bool:
        return True

    async def subscribe(self, channel: str):  # type: ignore[no-untyped-def]
        if False:
            yield ""  # pragma: no cover — async generator placeholder

    async def close(self) -> None:
        pass


# ── Notification stub ───────────────────────────────────────────────────────


class StubNotificationService:
    """Drop-in replacement for ``NotificationService`` — alerts are no-ops."""

    async def send_sla_alert(self, **kwargs: Any) -> None:
        pass
