"""Ingestion service — per-source CRUD, run pipeline, and event recording.

Spec: spec/feature/BACKEND.md §Ingestion Service
Schema: spec/feature/BACKEND_SCHEMA.md §ingestion_source + §ingestion_source_dataset
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from datahub.metadata.schema_classes import (
    AuditStampClass,
    DataProcessInstanceOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRelationshipsClass,
    DataProcessInstanceRunEventClass,
    DataProcessInstanceRunResultClass,
    DataProcessRunStatusClass,
    DataProcessTypeClass,
    RunResultTypeClass,
    SystemMetadataClass,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.extractors import run_extractor
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.datahub.urn import platform_from_dataset_urn
from src.shared.db.models import Event, IngestionSource, IngestionSourceDataset
from src.shared.db.registry import reconcile_registry
from src.shared.events import (
    INGESTION_COMPLETE,
    INGESTION_FAIL,
    INGESTION_PREFIX,
    INGESTION_SOURCE_CREATE,
    INGESTION_SOURCE_DELETE,
    INGESTION_SOURCE_UPDATE,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    PreconditionFailedError,
    StorageUnavailableError,
)
from src.shared.models.ingestion import (
    Mode,
    build_matcher_checked,
    cron_to_tier,
    extract_secret_refs,
    has_selection_patterns,
    parse_recipe,
    truncate_reason,
)
from src.shared.secrets import (
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
    resolve_recipe_secrets,
    verify_secret_ref,
)
from src.workflows._common import read_datahub_actor_urn, read_datahub_default_env

logger = logging.getLogger(__name__)

# DataHub execution-result status → DataSpoke event mapping.
# Only executions that reached a real ingestion outcome are mirrored:
#   SUCCESS / SUCCEEDED (cross-version)              → INGESTION_COMPLETE
#   FAILURE / TIMEOUT / ABORTED / ROLLBACK_FAILED    → INGESTION_FAIL
# Every other status is NOT mirrored:
#   RUNNING / ROLLING_BACK / UP_FOR_RETRY            → in-progress (may still resolve)
#   CANCELLED / DUPLICATE / ROLLED_BACK / empty      → not an ingestion outcome
_COMPLETE_STATUSES: frozenset[str] = frozenset({"SUCCESS", "SUCCEEDED"})
_FAIL_STATUSES: frozenset[str] = frozenset(
    {"FAILURE", "TIMEOUT", "ABORTED", "ROLLBACK_FAILED"}
)


# ── Value objects ─────────────────────────────────────────────────────────────


class IngestionSourceRecord(BaseModel):
    """Service-layer value object mirroring the ingestion_source ORM row.

    ``schedule_tier`` is internal (never in API responses) but returned here
    for DAG dispatch and metrics tier resolution.
    """

    id: str
    mode: str
    name: str
    platform: str
    recipe: dict[str, Any]
    schedule: str | None
    schedule_tier: str | None
    datahub_source_urn: str | None
    parent_source_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionRunResult(BaseModel):
    """Value object for the outcome of an ingestion run."""

    run_id: str
    status: str  # 'success' | 'error'
    dry_run: bool
    discovered_urns: list[str]
    emitted_urns: list[str]
    errors: list[str]
    warnings: list[str]


def run_report_detail(result: IngestionRunResult) -> dict[str, object]:
    """Build the flat discovered/emitted report keys shared by the run response
    and the ``INGESTION.COMPLETE``/``INGESTION.FAIL`` event detail.

    Returns only dataset-URN lists and their counts — never resolved credentials.
    """
    return {
        "discovered_urns": result.discovered_urns,
        "discovered_urns_count": len(result.discovered_urns),
        "emitted_urns": result.emitted_urns,
        "emitted_urns_count": len(result.emitted_urns),
    }


_DERIVATION_TO_AUTHORITY = {"emitted": "high", "pipeline_name": "high", "matched": "medium"}


class IngestionSourceDatasetRecord(BaseModel):
    """Value object for one ingestion_source_dataset row."""

    source_id: str
    dataset_urn: str
    derivation: str
    first_seen_at: datetime
    last_seen_at: datetime

    @property
    def authority(self) -> str:
        """Confidence in the source->dataset link, derived purely from ``derivation``.

        ``emitted``/``pipeline_name`` (observed) -> ``high``; ``matched`` (recipe
        filter inference) -> ``medium``.
        """
        return _DERIVATION_TO_AUTHORITY.get(self.derivation, "medium")


# ── ORM row converters ────────────────────────────────────────────────────────


def _source_from_row(row: IngestionSource) -> IngestionSourceRecord:
    return IngestionSourceRecord(
        id=str(row.id),
        mode=row.mode,
        name=row.name,
        platform=row.platform,
        recipe=dict(row.recipe) if row.recipe else {},
        schedule=row.schedule,
        schedule_tier=row.schedule_tier,
        datahub_source_urn=row.datahub_source_urn,
        parent_source_id=str(row.parent_source_id) if row.parent_source_id else None,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _dataset_from_row(row: IngestionSourceDataset) -> IngestionSourceDatasetRecord:
    return IngestionSourceDatasetRecord(
        source_id=str(row.source_id),
        dataset_urn=row.dataset_urn,
        derivation=row.derivation,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )


# Derivation priority for reverse-lookup winner selection (per spec):
# emitted > pipeline_name > matched.
_DERIVATION_PRIORITY = {"emitted": 0, "pipeline_name": 1, "matched": 2}


def _reverse_lookup_key(
    pair: tuple[IngestionSourceDataset, IngestionSource],
) -> tuple[int, int, float]:
    """Sort key selecting the single owning source for a dataset URN.

    Used only by :meth:`IngestionService.reverse_lookup` (single-winner
    selection): ``emitted`` > ``pipeline_name`` > ``matched``; a regular parent
    (``parent_source_id IS NULL`` → 0) wins over its CLI wrapper (1); within
    those ties the most recent ``last_seen_at`` sorts first.

    :meth:`IngestionService.reverse_lookup_all_batch` intentionally does NOT use
    this key — it returns every covering source sorted by ``(name, id)`` without
    applying the priority rule.
    """
    mapping, source = pair
    priority = _DERIVATION_PRIORITY.get(mapping.derivation, 99)
    is_wrapper = 1 if source.parent_source_id is not None else 0
    return (priority, is_wrapper, -mapping.last_seen_at.timestamp())


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_cli_wrapper(
    executor_id: str | None,
    source_urn: str | None,
    name: str | None,
) -> bool:
    """Identify a DATAHUB_MANAGED row as an auto-created CLI wrapper source.

    A row is a CLI wrapper when ANY of these markers holds (priority order):
      1. ``executor_id`` starts with ``__datahub_cli_`` (primary, decisive)
      2. the source URN id starts with ``cli-`` — the id is the last
         colon-segment of ``urn:li:dataHubIngestionSource:<id>``
      3. the display name starts with ``[CLI] ``

    This is the *detector* — it identifies a wrapper structurally, independent of
    how its parent is resolved. The parent link is resolved separately from the
    wrapper's ``recipe.pipeline_name`` field (see :meth:`sync` Pass B); an
    identified wrapper whose ``pipeline_name`` resolves to no stored regular parent
    is an orphan (not stored). Pure string parse — no DB or network access.
    """
    if executor_id and executor_id.startswith("__datahub_cli_"):
        return True
    if source_urn:
        source_id = source_urn.rsplit(":", 1)[-1]
        if source_id.startswith("cli-"):
            return True
    if name and name.startswith("[CLI] "):
        return True
    return False


def _validate_and_derive_tier(
    mode: str,
    schedule: str | None,
) -> str | None:
    """For ACTIVE_CUSTOM_MANAGED, validate schedule and return its tier.

    Returns None for null schedule (manual-only) or non-ACTIVE_CUSTOM_MANAGED
    modes (PASSIVE / DATAHUB_MANAGED have no tier constraint).

    Raises:
        ValueError: if mode == ACTIVE_CUSTOM_MANAGED and schedule is a non-None
            string that does not map to one of the three allowed tiers.
    """
    if mode != Mode.ACTIVE_CUSTOM_MANAGED.value:
        return None
    return cron_to_tier(schedule)


def _verify_recipe_secret_refs(recipe: dict[str, Any]) -> None:
    """Verify every ${name__key} ref in recipe.source.config exists in k8s.

    Raises:
        PreconditionFailedError(SECRET_REF_MALFORMED): malformed ref token.
        PreconditionFailedError(SECRET_REF_NOT_FOUND): missing secret/key.
    """
    refs = extract_secret_refs(recipe)
    for ref in refs:
        try:
            verify_secret_ref(ref)
        except SecretRefMalformed as exc:
            raise PreconditionFailedError(
                "SECRET_REF_MALFORMED",
                f"Secret reference '${{{ref}}}' is malformed: {exc}",
                detail={"ref": ref},
            ) from exc
        except SecretRefNotFound as exc:
            raise PreconditionFailedError(
                "SECRET_REF_NOT_FOUND",
                f"Secret reference '${{{ref}}}' not found: {exc}",
                detail={"ref": ref},
            ) from exc
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(f"Secret resolver unavailable: {exc}") from exc


def _reject_if_datahub_managed(mode: str, source_id: str) -> None:
    """Raise ConflictError if the source is DATAHUB_MANAGED (DataHub is SSOT)."""
    if mode == Mode.DATAHUB_MANAGED.value:
        raise ConflictError(
            "INGESTION_SOURCE_READONLY",
            f"Source {source_id!r} is DATAHUB_MANAGED and read-only in DataSpoke. "
            "Edit the source in DataHub directly.",
        )


# ── Sync sweep helpers ────────────────────────────────────────────────────────

# Known plaintext-secret keys that may appear in a DataHub recipe config.
# If DataHub returns the recipe with these keys set to non-${...} values,
# those values are masked.  Keys that already use ${...} refs are kept as-is
# because they do not contain actual secrets.
_SECRET_REF_RE_INLINE = re.compile(r"^\$\{[A-Za-z0-9_.-]+\}$")

_PLAINTEXT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "api_secret",
        "client_secret",
        "access_token",
        "secret_key",
        "private_key",
        "auth_token",
        "credential",
        "credentials",
        "aws_secret_access_key",
    }
)

_MASKED_VALUE = "********"


def _mask_recipe_secrets(recipe: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *recipe* with sensitive plaintext values masked.

    Masking rule:
      - Walk all string values in recipe.source.config recursively.
      - For each value whose key (case-insensitive) is in _PLAINTEXT_SECRET_KEYS:
        - If the value is already a ${...} reference → keep as-is.
        - Otherwise → replace with "********".
    - Values outside recipe.source.config (e.g. the type field) are never masked.

    This guards against DataHub returning raw credentials in the recipe JSON.
    """
    import copy

    masked = copy.deepcopy(recipe)
    config = (masked.get("source") or {}).get("config")
    if isinstance(config, dict):
        _mask_dict_inplace(config)
    return masked


def _mask_dict_inplace(obj: Any, *, _key: str | None = None) -> None:
    """Recursively mask plaintext secret values in-place."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                if k.lower() in _PLAINTEXT_SECRET_KEYS and not _SECRET_REF_RE_INLINE.match(v):
                    obj[k] = _MASKED_VALUE
                # else: leave as-is (either not a secret key, or already a ref)
            elif isinstance(v, (dict, list)):
                _mask_dict_inplace(v, _key=k)
    elif isinstance(obj, list):
        for item in obj:
            _mask_dict_inplace(item, _key=_key)


def _parse_recipe_str_safe(recipe_str: str) -> dict[str, Any]:
    """Parse a JSON recipe string safely; return empty dict on failure."""
    if not recipe_str:
        return {}
    try:
        parsed = json.loads(recipe_str)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _safe_parse_recipe(recipe: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """parse_recipe that returns ('unknown', {}) on failure."""
    try:
        return parse_recipe(recipe)
    except ValueError:
        return "unknown", {}


def _name_from_dataset_urn(urn: str) -> str | None:
    """Extract the name segment (second comma-field) from a dataset URN.

    Dataset URN format: ``urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<origin>)``

    The platform itself is a nested URN that contains a comma, so the first
    comma separates the platform URN from the name, and the last comma separates
    the name from the origin.  We use rfind to locate the last comma (origin
    boundary) and the second-to-last comma (platform/name boundary).

    Returns None for malformed URNs.
    """
    if not urn.startswith("urn:li:dataset:(") or not urn.endswith(")"):
        return None
    inner = urn[len("urn:li:dataset:(") : -1]
    last_comma = inner.rfind(",")
    if last_comma == -1:
        return None
    second_last_comma = inner.rfind(",", 0, last_comma)
    if second_last_comma == -1:
        return None
    name = inner[second_last_comma + 1 : last_comma].strip()
    return name if name else None


# ── Service ───────────────────────────────────────────────────────────────────


class IngestionService:
    """Per-source ingestion CRUD, run pipeline, and event recording."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient | None = None,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    # ── Source CRUD ──────────────────────────────────────────────────────────

    async def get_source(self, source_id: str) -> IngestionSourceRecord:
        """Return the source record for *source_id*.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(select(IngestionSource).where(IngestionSource.id == uid))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)
        return _source_from_row(row)

    async def list_sources(
        self,
        offset: int = 0,
        limit: int = 20,
        mode_filter: str | None = None,
        order_by: Any = None,
    ) -> tuple[list[IngestionSourceRecord], int]:
        """Return a paginated list of sources.

        CLI wrapper rows (``parent_source_id IS NOT NULL``) are internal plumbing
        and are never listed — their run events surface on the regular parent.

        Args:
            mode_filter: When provided, filter to sources with this mode value.
        """
        base = select(IngestionSource).where(IngestionSource.parent_source_id.is_(None))
        if mode_filter is not None:
            base = base.where(IngestionSource.mode == mode_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = IngestionSource.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()
        return [_source_from_row(r) for r in rows], total_count

    async def create_source(
        self,
        mode: str,
        name: str,
        schedule: str | None,
        recipe: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Create a new ingestion source.

        Validates:
        - recipe shape (parse_recipe)
        - every ${name__key} ref via verify_secret_ref (422 on failure)
        - schedule -> tier mapping for ACTIVE_CUSTOM_MANAGED (ValueError -> 422)

        DATAHUB_MANAGED rows are created only by the sync sweep (not via this
        method). Calling create_source with mode=DATAHUB_MANAGED raises
        ConflictError(INGESTION_SOURCE_READONLY) to enforce the SSOT split.

        Records INGESTION.SOURCE_CREATE event.
        """
        # Reject user-initiated creation of DATAHUB_MANAGED rows.
        _reject_if_datahub_managed(mode, "<new>")

        # Validate recipe shape.
        source_type, _ = parse_recipe(recipe)

        # Validate schedule -> tier for ACTIVE_CUSTOM_MANAGED.
        try:
            tier = _validate_and_derive_tier(mode, schedule)
        except ValueError as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                str(exc),
                detail={"schedule": schedule},
            ) from exc

        # Verify all secret refs at save time.
        _verify_recipe_secret_refs(recipe)

        row = IngestionSource(
            id=uuid.uuid4(),
            mode=mode,
            name=name,
            platform=source_type,
            recipe=recipe,
            schedule=schedule,
            schedule_tier=tier,
            status="OK",
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            str(row.id),
            INGESTION_SOURCE_CREATE,
            "success",
            {"name": name, "mode": mode, "platform": source_type},
        )
        return _source_from_row(row)

    async def replace_source(
        self,
        source_id: str,
        mode: str,
        name: str,
        schedule: str | None,
        recipe: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Full replacement of an existing source (PUT semantics).

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if mode is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(select(IngestionSource).where(IngestionSource.id == uid))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        source_type, _ = parse_recipe(recipe)

        try:
            tier = _validate_and_derive_tier(mode, schedule)
        except ValueError as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER", str(exc), detail={"schedule": schedule}
            ) from exc

        _verify_recipe_secret_refs(recipe)

        row.mode = mode
        row.name = name
        row.platform = source_type
        row.recipe = recipe
        row.schedule = schedule
        row.schedule_tier = tier
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_UPDATE,
            "success",
            {"name": name, "mode": mode, "platform": source_type, "operation": "PUT"},
        )
        return _source_from_row(row)

    async def patch_source(
        self,
        source_id: str,
        patch: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Partial update of an existing source (PATCH semantics).

        Accepted patch keys: ``name``, ``schedule``, ``recipe``.
        ``mode`` is not patchable — use replace_source for a mode change.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if the row is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(select(IngestionSource).where(IngestionSource.id == uid))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        if "name" in patch and patch["name"] is not None:
            row.name = patch["name"]

        if "recipe" in patch and patch["recipe"] is not None:
            new_recipe = patch["recipe"]
            source_type, _ = parse_recipe(new_recipe)
            _verify_recipe_secret_refs(new_recipe)
            row.recipe = new_recipe
            row.platform = source_type

        if "schedule" in patch:
            new_schedule = patch["schedule"]
            try:
                tier = _validate_and_derive_tier(row.mode, new_schedule)
            except ValueError as exc:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER", str(exc), detail={"schedule": new_schedule}
                ) from exc
            row.schedule = new_schedule
            row.schedule_tier = tier

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(patch.keys())},
        )
        return _source_from_row(row)

    async def delete_source(self, source_id: str) -> None:
        """Hard-delete a source (and cascades to ingestion_source_dataset).

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if the row is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(select(IngestionSource).where(IngestionSource.id == uid))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        mode = row.mode
        name = row.name
        await self._db.delete(row)
        await self._db.commit()

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_DELETE,
            "success",
            {"name": name, "mode": mode, "operation": "DELETE"},
        )

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        source_id: str,
        dry_run: bool = False,
        manual: bool = False,
    ) -> IngestionRunResult:
        """Run the full ingestion pipeline for an ACTIVE_CUSTOM_MANAGED source.

        Pipeline:
        1. Load source; reject if mode != ACTIVE_CUSTOM_MANAGED (409 INGESTION_RUN_NOT_APPLICABLE).
        2. Redis SETNX guard key ``ingestion:running:{source_id}`` (409 INGESTION_RUNNING).
        3. Resolve ``${name__key}`` recipe secrets into plaintext in-memory.
        4. Emit DataProcessInstance STARTED (non-dry-run). ``type`` is BATCH_AD_HOC
           when ``manual`` else BATCH_SCHEDULED.
        5. Dispatch to extractor for recipe.source.type.
        6. Emit DataProcessInstanceOutput(outputs=emitted_urns) after the crawl —
           non-dry-run, success, and non-empty emitted URNs only.
        7. Emit DataProcessInstance COMPLETE/FAILED (non-dry-run).
        8. Upsert emitted URNs into ingestion_source_dataset (derivation='emitted', non-dry-run).
        9. Record INGESTION.COMPLETE / INGESTION.FAIL event.

        Args:
            manual: True for manual ``sources/{id}/method/run`` invocations (DPI
                ``type=BATCH_AD_HOC``); False (default) for the scheduled tier-DAG
                path (DPI ``type=BATCH_SCHEDULED``).

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_RUN_NOT_APPLICABLE): non-ACTIVE_CUSTOM_MANAGED mode.
            ConflictError(INGESTION_RUNNING): concurrent run already in progress.
        """
        lock_key = f"ingestion:running:{source_id}"
        lock_token: str | None = None

        if self._cache is not None:
            lock_token = secrets.token_urlsafe(16)
            acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=3600)
            if not acquired:
                raise ConflictError(
                    "INGESTION_RUNNING",
                    f"Ingestion is already running for source {source_id}",
                )

        try:
            return await self._run_inner(source_id, dry_run, manual)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(
        self, source_id: str, dry_run: bool, manual: bool = False
    ) -> IngestionRunResult:
        """Inner run logic (executes inside the Redis SETNX guard)."""
        run_id = str(uuid.uuid4())

        source = await self.get_source(source_id)

        if source.mode != Mode.ACTIVE_CUSTOM_MANAGED.value:
            raise ConflictError(
                "INGESTION_RUN_NOT_APPLICABLE",
                f"Source {source_id!r} has mode={source.mode}. "
                "method/run is only valid for ACTIVE_CUSTOM_MANAGED sources.",
            )

        # Resolve secrets: plaintext only in-memory.
        try:
            resolved_recipe = resolve_recipe_secrets(source.recipe)
        except (SecretRefMalformed, SecretRefNotFound) as exc:
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "error": str(exc), "phase": "secret_resolution"},
            )
            return IngestionRunResult(
                run_id=run_id,
                status="error",
                dry_run=dry_run,
                discovered_urns=[],
                emitted_urns=[],
                errors=[str(exc)],
                warnings=[],
            )
        except SecretResolverUnavailable as exc:
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "error": str(exc), "phase": "secret_resolution"},
            )
            return IngestionRunResult(
                run_id=run_id,
                status="error",
                dry_run=dry_run,
                discovered_urns=[],
                emitted_urns=[],
                errors=[f"SecretResolverUnavailable: {exc}"],
                warnings=[],
            )

        # DPI URN and system metadata — stamped with source_id as pipelineName.
        dpi_urn = f"urn:li:dataProcessInstance:{source_id}-{run_id}"
        start_ms = int(time.time() * 1000)
        sysmeta = SystemMetadataClass(
            runId=f"dataspoke-{source_id}-{run_id}",
            pipelineName=source_id,
            lastObserved=start_ms,
        )

        # Emit DPI STARTED (non-dry-run only).
        if not dry_run:
            try:
                await self._emit_dpi_started(
                    dpi_urn, source, run_id, start_ms, sysmeta, manual=manual
                )
            except Exception as exc:
                logger.warning("DPI STARTED emission failed (non-fatal): %s", exc)

        # Run extractor.
        try:
            default_env = await read_datahub_default_env(self._db)
            ingestion_result = await run_extractor(
                datahub=self._datahub,
                source_id=source_id,
                recipe=resolved_recipe,
                dry_run=dry_run,
                run_id=run_id,
                default_env=default_env,
            )
        except Exception as exc:
            if not dry_run:
                await self._emit_dpi_terminal(
                    dpi_urn,
                    sysmeta,
                    start_ms=start_ms,
                    success=False,
                )
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "platform": source.platform, "exception": str(exc)},
            )
            raise

        errors = ingestion_result.errors
        warnings = ingestion_result.warnings

        # A non-dry-run that emitted zero datasets is treated as failure.
        if not dry_run and not ingestion_result.emitted_urns and not errors:
            errors = list(warnings) or ["No entities ingested from source"]

        status = "error" if errors else "success"

        # Emit DPI Output (aspect 2b) — dynamic-discovery extractors resolve their
        # target dataset URNs during the crawl, so this is emitted post-crawl,
        # after the success status is known and before the terminal RunEvent.
        # Skipped on dry-run, failure, and zero-entity runs.
        if not dry_run and status == "success" and ingestion_result.emitted_urns:
            await self._emit_dpi_output(dpi_urn, ingestion_result.emitted_urns, sysmeta)

        # Emit DPI terminal event (non-dry-run).
        if not dry_run:
            await self._emit_dpi_terminal(
                dpi_urn,
                sysmeta,
                start_ms=start_ms,
                success=(status == "success"),
            )

        # Record emitted URNs into ingestion_source_dataset (non-dry-run, success only).
        if not dry_run and status == "success" and ingestion_result.emitted_urns:
            await self._upsert_dataset_mappings(
                source_id=source_id,
                dataset_urns=ingestion_result.emitted_urns,
                derivation="emitted",
            )

        result = IngestionRunResult(
            run_id=run_id,
            status=status,
            dry_run=dry_run,
            discovered_urns=ingestion_result.discovered_urns,
            emitted_urns=ingestion_result.emitted_urns,
            errors=errors,
            warnings=warnings,
        )

        event_type = INGESTION_COMPLETE if status == "success" else INGESTION_FAIL
        await self._record_source_event(
            source_id,
            event_type,
            status,
            {
                "run_id": run_id,
                "platform": source.platform,
                "dry_run": dry_run,
                **run_report_detail(result),
                "errors": errors,
                "warnings": warnings,
            },
        )

        return result

    # ── DPI emission helpers ──────────────────────────────────────────────────

    async def _emit_dpi_started(
        self,
        dpi_urn: str,
        source: IngestionSourceRecord,
        run_id: str,
        start_ms: int,
        sysmeta: SystemMetadataClass,
        manual: bool = False,
    ) -> None:
        run_type = (
            DataProcessTypeClass.BATCH_AD_HOC if manual else DataProcessTypeClass.BATCH_SCHEDULED
        )
        actor_urn = await read_datahub_actor_urn(self._db)
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"dataspoke-{source.platform}-{run_id}",
                type=run_type,
                created=AuditStampClass(time=start_ms, actor=actor_urn),
            ),
            system_metadata=sysmeta,
        )
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstanceRelationshipsClass(
                upstreamInstances=[],
                parentTemplate=None,
            ),
            system_metadata=sysmeta,
        )
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.STARTED,
                timestampMillis=start_ms,
            ),
            system_metadata=sysmeta,
        )

    async def _emit_dpi_output(
        self,
        dpi_urn: str,
        emitted_urns: list[str],
        sysmeta: SystemMetadataClass,
    ) -> None:
        """Emit DataProcessInstanceOutput (aspect 2b) linking the DPI to the
        dataset(s) it ingested into, surfacing the run in DataHub's
        ``dataset(urn).runs`` query. Best-effort: a successful ingestion is not
        aborted if this aspect fails to emit.
        """
        try:
            await self._datahub.emit_aspect(
                dpi_urn,
                DataProcessInstanceOutputClass(outputs=emitted_urns),
                system_metadata=sysmeta,
            )
        except Exception as exc:
            logger.warning("DPI Output emission failed (non-fatal): %s", exc)

    async def _emit_dpi_terminal(
        self,
        dpi_urn: str,
        sysmeta: SystemMetadataClass,
        start_ms: int,
        success: bool,
    ) -> None:
        end_ms = int(time.time() * 1000)
        result_type = RunResultTypeClass.SUCCESS if success else RunResultTypeClass.FAILURE
        try:
            await self._datahub.emit_aspect(
                dpi_urn,
                DataProcessInstanceRunEventClass(
                    status=DataProcessRunStatusClass.COMPLETE,
                    timestampMillis=end_ms,
                    result=DataProcessInstanceRunResultClass(
                        type=result_type,
                        nativeResultType="dataspoke",
                    ),
                    durationMillis=end_ms - start_ms,
                ),
                system_metadata=sysmeta,
            )
        except Exception as exc:
            # Best-effort terminal emission; never let the failure path raise.
            logger.warning("DPI terminal emission failed (best-effort): %s", exc)

    # ── Dataset mapping helpers ───────────────────────────────────────────────

    async def _upsert_dataset_mappings(
        self,
        source_id: str,
        dataset_urns: list[str],
        derivation: str,
    ) -> None:
        """Upsert dataset URNs into ingestion_source_dataset.

        On insert: sets both first_seen_at and last_seen_at to now().
        On conflict (same source_id + dataset_urn): updates last_seen_at.
        """
        if not dataset_urns:
            return

        uid = uuid.UUID(source_id)
        now = datetime.now(tz=UTC)

        for dataset_urn in dataset_urns:
            stmt = (
                pg_insert(IngestionSourceDataset)
                .values(
                    source_id=uid,
                    dataset_urn=dataset_urn,
                    derivation=derivation,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["source_id", "dataset_urn"],
                    set_={"last_seen_at": now},
                )
            )
            await self._db.execute(stmt)

        await self._db.commit()

    # ── Tier DAG support ─────────────────────────────────────────────────────

    async def list_active_sources_for_tier(self, tier: str) -> list[IngestionSourceRecord]:
        """Return ACTIVE_CUSTOM_MANAGED sources for the given schedule tier.

        Used by the ingestion-active-{hourly,daily,weekly} Airflow DAGs.
        Sources with schedule=None (manual-only) are excluded.
        """
        result = await self._db.execute(
            select(IngestionSource).where(
                IngestionSource.mode == Mode.ACTIVE_CUSTOM_MANAGED.value,
                IngestionSource.schedule_tier == tier,
            )
        )
        return [_source_from_row(r) for r in result.scalars().all()]

    # ── Dataset mapping queries ───────────────────────────────────────────────

    async def list_datasets_for_source(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[IngestionSourceDatasetRecord], int]:
        """List dataset mappings for a given source.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if source not found.
        """
        await self.get_source(source_id)  # raises if not found
        uid = uuid.UUID(source_id)

        count_q = select(func.count()).where(IngestionSourceDataset.source_id == uid)
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = IngestionSourceDataset.last_seen_at.desc()
        rows_q = (
            select(IngestionSourceDataset)
            .where(IngestionSourceDataset.source_id == uid)
            .order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()
        return [_dataset_from_row(r) for r in rows], total_count

    async def reverse_lookup(
        self,
        dataset_urn: str,
    ) -> IngestionSourceRecord | None:
        """Return the owning source for a dataset URN.

        Priority rule (per spec): ``emitted`` > ``pipeline_name`` > ``matched``.
        When the same dataset is mapped at the same derivation priority by both a
        regular parent and its CLI wrapper, the regular parent wins so that the
        latest-run aggregation roots at the parent. Within those tiebreaks, the
        most recent ``last_seen_at`` wins.

        Returns None when no source claims this dataset.
        """
        result = await self._db.execute(
            select(IngestionSourceDataset, IngestionSource)
            .join(IngestionSource, IngestionSourceDataset.source_id == IngestionSource.id)
            .where(IngestionSourceDataset.dataset_urn == dataset_urn)
        )
        rows = result.all()
        if not rows:
            return None

        _best_mapping, best_source = sorted(rows, key=_reverse_lookup_key)[0]  # type: ignore[arg-type]  # SQLAlchemy Row key-func typing is imprecise.

        # Wrappers are internal plumbing and must never be surfaced as the owning
        # source. If a wrapper wins (e.g. it claims a dataset its parent does not),
        # resolve up to its regular parent so dataset-facing endpoints only ever
        # expose a regular source.
        if best_source.parent_source_id is not None:
            parent = await self._db.get(IngestionSource, best_source.parent_source_id)
            if parent is not None:
                best_source = parent

        return _source_from_row(best_source)

    async def reverse_lookup_all_batch(
        self,
        urns: list[str],
    ) -> dict[str, list[IngestionSourceRecord]]:
        """Batched all-sources reverse-lookup — **every** covering source per URN.

        Unlike :meth:`reverse_lookup` (single priority winner), this returns
        ALL sources covering each URN, since ``ingestion_source_dataset`` is keyed
        ``(source_id, dataset_urn)`` and a dataset may be claimed by several
        sources. Two queries bound the cost regardless of page size:

        1. one mapping⋈source join filtered by ``dataset_urn IN urns``;
        2. one ``IngestionSource.id IN parent_ids`` query resolving any winning
           wrapper up to its regular parent.

        CLI wrappers are internal plumbing and are never surfaced: each covering
        wrapper is resolved up to its regular parent, then results are
        de-duplicated by source id (a parent reached via several wrappers, or
        directly, appears once). Each list is sorted by source ``name`` then ``id``
        for deterministic ordering.

        Returns a dict keyed by every URN in ``urns``; the value is ``[]`` when no
        source claims that URN (so callers can read every URN unconditionally).
        """
        result: dict[str, list[IngestionSourceRecord]] = {urn: [] for urn in urns}
        if not urns:
            return result

        rows_result = await self._db.execute(
            select(IngestionSourceDataset, IngestionSource)
            .join(IngestionSource, IngestionSourceDataset.source_id == IngestionSource.id)
            .where(IngestionSourceDataset.dataset_urn.in_(urns))
        )
        rows = rows_result.all()
        if not rows:
            return result

        # Resolve every covering wrapper to its regular parent in ONE query.
        parent_ids: set[uuid.UUID] = {
            source.parent_source_id
            for _mapping, source in rows
            if source.parent_source_id is not None
        }
        parents: dict[uuid.UUID, IngestionSource] = {}
        if parent_ids:
            parent_result = await self._db.execute(
                select(IngestionSource).where(IngestionSource.id.in_(parent_ids))
            )
            parents = {p.id: p for p in parent_result.scalars().all()}

        # Group resolved sources per URN, de-duplicating by resolved source id.
        seen: dict[str, set[str]] = {urn: set() for urn in urns}
        for mapping, source in rows:
            resolved = source
            if source.parent_source_id is not None:
                parent = parents.get(source.parent_source_id)
                if parent is not None:
                    resolved = parent
            sid = str(resolved.id)
            urn = mapping.dataset_urn
            if sid in seen[urn]:
                continue
            seen[urn].add(sid)
            result[urn].append(_source_from_row(resolved))

        for sources in result.values():
            sources.sort(key=lambda r: (r.name, r.id))

        return result

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events_for_source(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ingestion events for a source (paginated).

        Events are stored with ``entity_type='ingestion_source'`` and
        ``entity_id=source_id``. For a regular source the union also includes
        events recorded on its linked CLI wrapper rows
        (``parent_source_id = source_id``) — DataHub books a managed source's runs
        on the wrapper, not the parent. Each returned row carries a derived
        ``wrapper`` flag (``True`` when the event's ``entity_id`` is a wrapper,
        i.e. not this source's own id).
        """
        # Resolve linked wrapper ids; their run events surface on this source.
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            # Non-UUID source_id can match no stored row; fall back to the raw
            # string so the (empty-result) query still runs without children.
            canonical = source_id
            child_ids: list[str] = []
        else:
            # Stored entity_id values are canonical str(uuid); normalize so a
            # non-canonical (e.g. uppercase) path param still matches the parent's
            # own events and does not mis-flag them as wrapper rows.
            canonical = str(uid)
            child_result = await self._db.execute(
                select(IngestionSource.id).where(IngestionSource.parent_source_id == uid)
            )
            child_ids = [str(cid) for cid in child_result.scalars().all()]

        entity_ids = [canonical, *child_ids]

        base = select(Event).where(
            Event.entity_type == "ingestion_source",
            Event.entity_id.in_(entity_ids),
            Event.event_type.startswith(INGESTION_PREFIX),
        )
        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = Event.occurred_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "event_type": row.event_type,
                "status": row.status,
                "detail": row.detail,
                "occurred_at": row.occurred_at,
                "wrapper": row.entity_id != canonical,
            }
            for row in rows
        ], total_count

    async def _record_source_event(
        self,
        source_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="ingestion_source",
            entity_id=source_id,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()

    # ── Sync sweep (Phase 2b) ─────────────────────────────────────────────────

    async def sync(self) -> dict[str, Any]:
        """Reconcile all ingestion sources against DataHub.

        Five-step pipeline (per spec/feature/BACKEND.md §Sync + mapping sweep):

        1. **Source defs**: pull DATAHUB_MANAGED source recipes + schedules via
           listIngestionSources; upsert read-only rows; remove stale rows.
        2. **Mapping + registry reconcile**: enumerate all DataHub datasets once,
           rebuild ingestion_source_dataset rows with derivation='matched' for every
           source. Preserve derivation='emitted' rows (authoritative from active-custom
           runs). A source whose pattern set never ran — the recipe could not be read,
           a pattern key holds a value the matcher cannot read, or the matcher raised —
           keeps its stored matched rows for this sweep; they are not pruned against an
           unevaluated pattern, and the source is counted in
           ``sources_pattern_degraded``.
           Reconcile dataset_registry to mirror the full DataHub URN set:
           insert new URNs as registered, soft-flag removed URNs as unregistered.
        3. **Observed enrichment**: for DATAHUB_MANAGED and ACTIVE_CUSTOM_MANAGED
           sources, read systemMetadata.pipelineName per dataset and upsert
           derivation='pipeline_name' rows where the name matches a source.
        4. **Run events**: mirror terminal execution requests for DATAHUB_MANAGED
           sources as INGESTION.COMPLETE / INGESTION.FAIL events.
           For PASSIVE sources, observe Operation timeseries on mapped datasets.
        5. **Unmanaged bucket**: served on-read by the router — sync() does not
           persist a separate table.

        Returns:
            Summary dict for the activity endpoint / logging. Every counter but
            ``sources_synced`` and ``sources_zero_coverage`` reports **state
            changes**, so a second consecutive sweep over an unchanged estate
            returns zero for all of them:
              sources_synced    — DATAHUB_MANAGED rows mirrored (inserts and updates
                                  alike; a steady-state reading, not a delta)
              sources_removed   — DATAHUB_MANAGED rows removed (gone from DataHub)
              datasets_mapped   — new ingestion_source_dataset matched rows inserted
              pipeline_links    — new pipeline_name rows, plus matched → pipeline_name
                                  upgrades; a re-confirmed pipeline_name row still
                                  refreshes last_seen_at but does not count
              events_mirrored   — new INGESTION events written
              registry_inserted — new dataset_registry rows inserted (datahub_registered=True)
              registry_marked_true   — existing rows flipped from False to True
              registry_marked_false  — existing rows soft-flagged as False (left DataHub)
              sources_zero_coverage  — sources whose selection patterns are derivable
                                  and well-formed but matched no dataset while DataHub
                                  holds datasets for their platform (a condition, so it
                                  stays non-zero for as long as the affected sources do).
                                  Counted once per registered source — a CLI wrapper
                                  mirrors its parent's recipe and is not counted again.
              sources_pattern_degraded — sources whose pattern set was never evaluated
                                  this sweep: the recipe could not be read, a
                                  selection-pattern key is wrongly shaped, a pattern does
                                  not compile, or the matcher raised while running. Their
                                  stored ``matched`` rows are kept rather than pruned, so
                                  this counter is the wire signal that some of the
                                  mapping set is stale rather than reconciled. Also a
                                  condition, not a delta. Counted per source row
                                  including CLI wrappers — unlike zero coverage this is
                                  not a shared recipe misconfiguration but a per-row
                                  prune that was skipped, and each wrapper holds its own
                                  mapping set that the skip protects.
        """
        summary: dict[str, Any] = {
            "sources_synced": 0,
            "sources_removed": 0,
            "datasets_mapped": 0,
            "pipeline_links": 0,
            "events_mirrored": 0,
            "registry_inserted": 0,
            "registry_marked_true": 0,
            "registry_marked_false": 0,
            "sources_zero_coverage": 0,
            "sources_pattern_degraded": 0,
        }

        # ── Step 1: Source defs (DATAHUB_MANAGED) ────────────────────────────
        # Two-pass sync/reconcile. DataHub books a managed source's *executions*
        # on an auto-created CLI wrapper source (`[CLI] <type> [<parent-urn>]`),
        # not on the regular source. We store wrappers as internal plumbing linked
        # to their regular parent via parent_source_id, and surface their run
        # events on the parent. A wrapper with no resolvable regular parent is a
        # stale orphan — not stored, not marked seen → dropped by stale-removal.
        dh_sources = await self._datahub.list_ingestion_sources()
        seen_urns: set[str] = set()

        # parent_by_urn: regular DATAHUB_MANAGED source datahub_source_urn → row id.
        parent_by_urn: dict[str, uuid.UUID] = {}
        # Defer wrapper rows to Pass B so parent ordering within the list is moot.
        wrapper_rows: list[dict[str, Any]] = []

        async def _upsert_managed_row(
            s: dict[str, Any], parent_source_id: uuid.UUID | None
        ) -> uuid.UUID:
            """Upsert one DATAHUB_MANAGED row, matched on datahub_source_urn.

            Returns the row id. ``parent_source_id`` links a wrapper to its
            regular parent (None for a regular source).
            """
            source_urn = s["urn"]
            recipe_str = s.get("recipe") or ""
            recipe_dict = _parse_recipe_str_safe(recipe_str)
            masked_recipe = _mask_recipe_secrets(recipe_dict)
            source_type, _ = _safe_parse_recipe(masked_recipe)
            schedule_interval: str | None = (s.get("schedule") or {}).get("interval") or None
            try:
                tier = cron_to_tier(schedule_interval)
            except ValueError:
                tier = None

            result = await self._db.execute(
                select(IngestionSource).where(
                    IngestionSource.datahub_source_urn == source_urn,
                    IngestionSource.mode == Mode.DATAHUB_MANAGED.value,
                )
            )
            row = result.scalar_one_or_none()
            now = datetime.now(tz=UTC)

            if row is None:
                row = IngestionSource(
                    id=uuid.uuid4(),
                    mode=Mode.DATAHUB_MANAGED.value,
                    name=s.get("name") or source_urn,
                    platform=source_type,
                    recipe=masked_recipe,
                    schedule=schedule_interval,
                    schedule_tier=tier,
                    datahub_source_urn=source_urn,
                    parent_source_id=parent_source_id,
                    status="OK",
                )
                self._db.add(row)
            else:
                row.name = s.get("name") or source_urn
                row.platform = source_type
                row.recipe = masked_recipe
                row.schedule = schedule_interval
                row.schedule_tier = tier
                row.parent_source_id = parent_source_id
                row.status = "OK"
                row.updated_at = now
                self._db.add(row)
            summary["sources_synced"] += 1
            return row.id

        # Pass A — regular sources (rows NOT identified as CLI wrappers).
        # Wrapper-ness is determined by marker (_is_cli_wrapper), independent of
        # whether the name encodes a resolvable parent URN; deferred wrappers are
        # resolved in Pass B.
        for s in dh_sources:
            source_urn = s.get("urn") or ""
            if not source_urn:
                continue
            if _is_cli_wrapper(s.get("executor_id"), source_urn, s.get("name")):
                wrapper_rows.append(s)
                continue
            seen_urns.add(source_urn)
            row_id = await _upsert_managed_row(s, parent_source_id=None)
            parent_by_urn[source_urn] = row_id

        await self._db.commit()

        # Pass B — wrappers: resolve the parent URN from the wrapper's reported
        # recipe top-level `pipeline_name` field (DataHub sets it to the registered
        # parent's source URN) and store only when it matches a regular
        # DATAHUB_MANAGED row from Pass A. A wrapper whose recipe carries no
        # `pipeline_name`, or one whose `pipeline_name` resolves to no stored
        # regular parent, is an orphan: skipped (not marked seen) so stale-removal
        # drops it. The cosmetic display name is never used for linking.
        for s in wrapper_rows:
            source_urn = s["urn"]
            recipe_dict = _parse_recipe_str_safe(s.get("recipe") or "")
            parent_urn = recipe_dict.get("pipeline_name")
            parent_id = parent_by_urn.get(parent_urn) if parent_urn else None
            if parent_id is None:
                continue
            seen_urns.add(source_urn)
            await _upsert_managed_row(s, parent_source_id=parent_id)

        await self._db.commit()

        # Remove DATAHUB_MANAGED rows whose source URN is no longer in DataHub
        # (removed/stale sources and orphan wrappers both drop out). ON DELETE
        # CASCADE removes a parent's wrappers automatically; the explicit delete
        # loop tolerates already-cascaded children (benign no-op).
        result = await self._db.execute(
            select(IngestionSource).where(IngestionSource.mode == Mode.DATAHUB_MANAGED.value)
        )
        all_managed_rows = result.scalars().all()
        for row in all_managed_rows:
            if row.datahub_source_urn and row.datahub_source_urn not in seen_urns:
                await self._db.delete(row)
                summary["sources_removed"] += 1

        if summary["sources_removed"]:
            await self._db.commit()

        # ── Step 2: Mapping (all modes) ───────────────────────────────────────
        # Enumerate the full dataset set once.
        all_dataset_urns = await self._datahub.enumerate_datasets()

        # Build two lookups from the same single enumeration:
        #   urn -> name segment (second comma-field of the URN inner tuple), and
        #   urn -> platform id (first comma-field). URNs the helpers can't parse
        #   are skipped from both maps.
        urn_to_name: dict[str, str] = {}
        urn_to_platform: dict[str, str] = {}
        for urn in all_dataset_urns:
            name = _name_from_dataset_urn(urn)
            if name:
                urn_to_name[urn] = name
            platform = platform_from_dataset_urn(urn)
            if platform:
                urn_to_platform[urn] = platform

        # Candidate count per platform, computed once over the set the matcher is
        # actually offered — URNs present in *both* maps, since a name that failed to
        # parse is never evaluated. A source whose platform is absent here had no
        # candidate at all, so its empty match set is not a defect signal.
        platform_dataset_counts: Counter[str] = Counter(
            urn_to_platform[urn] for urn in urn_to_name if urn in urn_to_platform
        )

        # Load all source rows to evaluate matchers.
        result = await self._db.execute(select(IngestionSource))
        all_sources_rows = result.scalars().all()

        for src_row in all_sources_rows:
            source_id = src_row.id
            recipe = dict(src_row.recipe) if src_row.recipe else {}

            # Platform scoping is the sweep's responsibility (the matcher sees only
            # names): require the URN platform to equal the recipe's source.type
            # before evaluating the name matcher.
            expected_platform = _safe_parse_recipe(recipe)[0].lower()
            # The reason is None only when the pattern set is well-shaped and compiles;
            # it names the offending config key otherwise. Taking it here rather than
            # from build_matcher is what lets this sweep tell "matched nothing" apart
            # from "could not be evaluated" — and log it with the source attached.
            matcher, degraded_reason = build_matcher_checked(recipe)
            matcher_failed = False
            try:
                matched_urns: set[str] = {
                    urn
                    for urn, name in urn_to_name.items()
                    if urn_to_platform.get(urn) == expected_platform and matcher(name)
                }
            except Exception:
                # One source's pattern must never abort the sweep for every other
                # source: log, degrade this source to match-nothing, carry on.
                # build_matcher_checked reports malformed and wrongly-typed patterns
                # as a reason, so what reaches here failed at match time. Pattern
                # *execution* time is not bounded — a catastrophically backtracking
                # regex never raises, and it is synchronous CPU work inside this
                # ``async def``: on the single-worker uvicorn process that serves the
                # API it stalls the event loop, so no request of any kind is handled
                # until the /health liveness probe gives up and the pod is restarted.
                # (asyncio.to_thread is no mitigation: CPython holds the GIL for the
                # whole of one re match.) Tracked as issue #114.
                matcher_failed = True
                logger.exception(
                    "ingestion_sync_matcher_failed — source_id=%s name=%r: "
                    "treating as match-nothing for this sweep",
                    source_id,
                    src_row.name,
                )
                matched_urns = set()

            # This source's pattern set was never evaluated over the estate: either it
            # could not be built (the recipe could not be read at all, or — ``recipe``
            # being writer-supplied JSONB — a pattern key holds a bare string or a
            # pattern that does not compile) or the matcher raised while running. An
            # empty match set is evidence that datasets stopped matching only when the
            # matcher actually ran over a well-formed pattern set; a degraded source is
            # absence of evidence, so it neither reports coverage nor prunes below.
            # It is counted instead, so the caller sees on the wire that part of the
            # mapping set went unreconciled rather than reading a clean sweep.
            patterns_degraded = matcher_failed or degraded_reason is not None
            if patterns_degraded:
                summary["sources_pattern_degraded"] += 1
            if degraded_reason is not None:
                # %r + truncate_reason: the reason quotes writer-supplied recipe text,
                # which is unbounded and may contain a real newline that would split
                # this record for a line-based collector.
                logger.warning(
                    "ingestion_sync_pattern_not_derivable — source_id=%s name=%r: %r "
                    "— this source matches nothing this sweep and its stored matched "
                    "rows are kept rather than pruned",
                    source_id,
                    src_row.name,
                    truncate_reason(degraded_reason),
                )

            # Zero coverage: a well-formed, derivable pattern set that matched
            # nothing while this platform did offer candidates. A source with no
            # derivable patterns legitimately maps nothing and is not reported, and
            # a degraded one is a recipe/matcher defect carrying its own warning —
            # reporting it here too would double-count it as a coverage signal.
            # CLI wrappers are skipped: a wrapper mirrors its parent's recipe, so
            # counting it too would report one misconfiguration twice.
            if (
                not matched_urns
                and not patterns_degraded
                and src_row.parent_source_id is None
                and has_selection_patterns(recipe)
                and expected_platform in platform_dataset_counts
            ):
                logger.warning(
                    "ingestion_sync_zero_coverage — source_id=%s name=%r platform=%r: "
                    "selection patterns are derivable but matched none of the %d "
                    "datasets DataHub holds for this platform",
                    source_id,
                    src_row.name,
                    expected_platform,
                    platform_dataset_counts[expected_platform],
                )
                summary["sources_zero_coverage"] += 1

            # Fetch currently stored matched-derivation rows for this source.
            existing_result = await self._db.execute(
                select(IngestionSourceDataset).where(
                    IngestionSourceDataset.source_id == source_id,
                    IngestionSourceDataset.derivation == "matched",
                )
            )
            existing_matcher_rows: dict[str, IngestionSourceDataset] = {
                r.dataset_urn: r for r in existing_result.scalars().all()
            }

            now = datetime.now(tz=UTC)

            # Upsert matched rows for currently-matched datasets.
            # F1+F2: use pg_insert with a WHERE guard on the conflict path so that:
            #   - A conflict against an existing emitted/pipeline_name row is a no-op
            #     (the higher-precedence row is never overwritten or demoted).
            #   - A conflict against an existing matched row just bumps last_seen_at.
            #   - A genuinely new row is inserted with derivation='matched'.
            for urn in matched_urns:
                stmt = (
                    pg_insert(IngestionSourceDataset)
                    .values(
                        source_id=source_id,
                        dataset_urn=urn,
                        derivation="matched",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["source_id", "dataset_urn"],
                        set_={"last_seen_at": now},
                        where=(IngestionSourceDataset.derivation == "matched"),
                    )
                )
                insert_result = await self._db.execute(stmt)
                # rowcount == 1 on INSERT, 1 on UPDATE, 0 when the WHERE filtered
                # out the conflict update (higher-precedence row untouched).
                if insert_result.rowcount == 1 and urn not in existing_matcher_rows:  # type: ignore[attr-defined]  # Result.rowcount exists at runtime.
                    summary["datasets_mapped"] += 1

            # Prune stale matched rows (derivation='matched') that no longer match.
            # These rows were fetched with derivation=='matched' filter above, so this
            # loop can only delete matched-derivation rows — emitted/pipeline_name rows
            # are never in existing_matcher_rows and are therefore never deleted here.
            #
            # Skipped for a degraded pattern set (the matcher raised, or could not be
            # built over the recipe at all): the empty match set is then an absence of
            # evidence, not evidence that the stored rows stopped matching, so pruning
            # on it would drop this source's entire mapping set over one bad pattern.
            # Keep them and let the next sweep, over a pattern set that actually ran,
            # decide.
            if not patterns_degraded:
                for urn, stale_row in existing_matcher_rows.items():
                    if urn not in matched_urns:
                        await self._db.delete(stale_row)

        await self._db.commit()

        # ── Step 2b: Registry reconcile ───────────────────────────────────────
        # Uses the same all_dataset_urns enumerated above — no second DataHub call.
        # Committed independently so a reconcile error does not roll back the mapping.
        try:
            reg_counts = await reconcile_registry(self._db, set(all_dataset_urns))
            await self._db.commit()
            summary["registry_inserted"] = reg_counts["inserted"]
            summary["registry_marked_true"] = reg_counts["marked_true"]
            summary["registry_marked_false"] = reg_counts["marked_false"]
        except Exception:
            logger.exception("registry_reconcile_failed — skipping, will retry next sweep")
            await self._db.rollback()

        # ── Step 3: Observed enrichment (MANAGED modes) ───────────────────────
        if all_dataset_urns:
            pipeline_map = await self._datahub.get_pipeline_names(all_dataset_urns)

            # Build lookup: pipeline_name -> [source_id, …] for MANAGED sources
            # (1:many — a single pipelineName can award pipeline_name/high to both
            # a registered source and the CLI wrapper that inherits from it).
            # DATAHUB_MANAGED registered (parent_source_id IS NULL): pipelineName ==
            #   datahub_source_urn (DataHub stamps the registered source URN on
            #   emitted aspects).
            # DATAHUB_MANAGED CLI wrapper (parent_source_id IS NOT NULL): a
            #   `datahub ingest` run of a registered source stamps aspects with
            #   pipelineName = parent registered URN. The wrapper inherits the link
            #   via its stored parent's datahub_source_urn (resolved through the
            #   id_to_urn map below) — no name re-parsing.
            # ACTIVE_CUSTOM_MANAGED: pipelineName == str(source.id) (DataSpoke extractor
            #   stamps pipelineName = source_id per the DPI emission convention).
            id_to_urn: dict[uuid.UUID, str] = {
                row.id: row.datahub_source_urn
                for row in all_sources_rows
                if row.mode == Mode.DATAHUB_MANAGED.value
                and row.parent_source_id is None
                and row.datahub_source_urn
            }

            pipeline_to_sources: dict[str, list[uuid.UUID]] = {}
            for src_row in all_sources_rows:
                if src_row.mode == Mode.DATAHUB_MANAGED.value:
                    if src_row.parent_source_id is None:
                        if src_row.datahub_source_urn:
                            pipeline_to_sources.setdefault(src_row.datahub_source_urn, []).append(
                                src_row.id
                            )
                    else:
                        # CLI wrapper: inherit via the stored parent link. The
                        # parent's datahub_source_urn is the pipelineName DataHub
                        # stamps. When the parent is missing from id_to_urn (e.g.
                        # cascaded away mid-sweep), inherit nothing — keep the
                        # step-2 matched/medium mapping.
                        parent_urn = id_to_urn.get(src_row.parent_source_id)
                        if parent_urn:
                            pipeline_to_sources.setdefault(parent_urn, []).append(src_row.id)
                elif src_row.mode == Mode.ACTIVE_CUSTOM_MANAGED.value:
                    pipeline_to_sources.setdefault(str(src_row.id), []).append(src_row.id)

            # Existing pipeline_name rows for the sources about to be upserted,
            # read once before the loop (mirrors step 2's existing_matcher_rows).
            # The upsert's ON CONFLICT DO UPDATE reports rowcount == 1 even when it
            # only re-confirms an unchanged row, so distinguishing a new link (or a
            # matched → pipeline_name upgrade) from a re-confirmation needs the
            # prior state.
            existing_pipeline_keys: set[tuple[uuid.UUID, str]] = set()
            if pipeline_to_sources:
                candidate_source_ids = {
                    sid for sids in pipeline_to_sources.values() for sid in sids
                }
                existing_pipeline_result = await self._db.execute(
                    select(
                        IngestionSourceDataset.source_id,
                        IngestionSourceDataset.dataset_urn,
                    ).where(
                        IngestionSourceDataset.source_id.in_(candidate_source_ids),
                        IngestionSourceDataset.derivation == "pipeline_name",
                    )
                )
                existing_pipeline_keys = {
                    (row_source_id, row_dataset_urn)
                    for row_source_id, row_dataset_urn in existing_pipeline_result.all()
                }

            now = datetime.now(tz=UTC)
            for dataset_urn, pipeline_name in pipeline_map.items():
                if not pipeline_name:
                    continue
                for source_id in pipeline_to_sources.get(pipeline_name, []):
                    stmt = (
                        pg_insert(IngestionSourceDataset)
                        .values(
                            source_id=source_id,
                            dataset_urn=dataset_urn,
                            derivation="pipeline_name",
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=["source_id", "dataset_urn"],
                            # F3: do not demote an emitted row to pipeline_name.
                            # derivation is overwritten to pipeline_name only when the
                            # WHERE guard passes; emitted rows are excluded so they stay
                            # pristine (neither derivation nor last_seen_at bumped).
                            set_={"derivation": "pipeline_name", "last_seen_at": now},
                            where=(IngestionSourceDataset.derivation != "emitted"),
                        )
                    )
                    insert_result = await self._db.execute(stmt)
                    # rowcount == 1 on INSERT, 1 on UPDATE, 0 when the WHERE guard
                    # filtered out the conflict update (an emitted row shadows the
                    # slot). Count state changes only: a new link or a genuine
                    # matched → pipeline_name upgrade. Re-confirming an existing
                    # pipeline_name row still refreshes last_seen_at — reverse_lookup's
                    # tie-break, the ingestion-freshness measurer and the mappings list
                    # ordering all read it — but is not a change, so it does not count.
                    if (
                        insert_result.rowcount == 1  # type: ignore[attr-defined]  # Result.rowcount exists at runtime.
                        and (source_id, dataset_urn) not in existing_pipeline_keys
                    ):
                        summary["pipeline_links"] += 1

            await self._db.commit()

        # ── Step 4: Run events ────────────────────────────────────────────────
        # Mirror DATAHUB_MANAGED terminal executions into the events table.
        for src_row in all_sources_rows:
            if src_row.mode == Mode.DATAHUB_MANAGED.value and src_row.datahub_source_urn:
                mirrored = await self._mirror_execution_requests(
                    source_id=str(src_row.id),
                    datahub_source_urn=src_row.datahub_source_urn,
                )
                summary["events_mirrored"] += mirrored

        # PASSIVE: observe Operation timeseries on mapped datasets (best-effort).
        for src_row in all_sources_rows:
            if src_row.mode == Mode.PASSIVE.value:
                mirrored = await self._observe_passive_operations(str(src_row.id))
                summary["events_mirrored"] += mirrored

        return summary

    # ── Sync helpers ──────────────────────────────────────────────────────────

    async def _mirror_execution_requests(
        self,
        source_id: str,
        datahub_source_urn: str,
    ) -> int:
        """Mirror execution requests that reached an ingestion outcome.

        Only executions whose status maps to a real outcome are mirrored:
        SUCCESS/SUCCEEDED → COMPLETE, FAILURE/TIMEOUT/ABORTED/ROLLBACK_FAILED →
        FAIL.  In-progress (RUNNING/ROLLING_BACK/UP_FOR_RETRY) and non-outcome
        (CANCELLED/DUPLICATE/ROLLED_BACK/empty) statuses produce no event.

        Identity is the execution-request URN: each request yields at most one
        event, upserted (looked up via ``detail->>'execution_request_urn'`` for
        this source) so repeated syncs and status transitions never duplicate.

        ``occurred_at`` is ``startTimeMs`` when present (>0), else ``requestedAt``;
        never ``now()``.

        Returns the count of newly inserted events.
        """
        try:
            requests = await self._datahub.list_execution_requests(datahub_source_urn)
        except Exception as exc:
            logger.warning("list_execution_requests failed for %s: %s", datahub_source_urn, exc)
            return 0

        inserted = 0
        for req in requests:
            status_str = req.get("status") or ""
            if status_str in _COMPLETE_STATUSES:
                event_type = INGESTION_COMPLETE
            elif status_str in _FAIL_STATUSES:
                event_type = INGESTION_FAIL
            else:
                # In-progress or non-outcome status — not mirrored.
                continue

            urn = req.get("urn") or ""
            if not urn:
                continue

            # Idempotency: one event per execution-request URN for this source.
            dup_result = await self._db.execute(
                select(Event.id).where(
                    Event.entity_type == "ingestion_source",
                    Event.entity_id == source_id,
                    Event.detail["execution_request_urn"].astext == urn,
                )
            )
            if dup_result.first() is not None:
                continue

            start_ms = req.get("startTimeMs")
            if start_ms is not None and start_ms > 0:
                occurred_at = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
            else:
                requested_at = req.get("requestedAt")
                occurred_at = datetime.fromtimestamp((requested_at or 0) / 1000, tz=UTC)

            self._db.add(
                Event(
                    entity_type="ingestion_source",
                    entity_id=source_id,
                    event_type=event_type,
                    status="success" if event_type == INGESTION_COMPLETE else "failure",
                    detail={
                        "execution_request_urn": urn,
                        "duration_ms": req.get("durationMs"),
                        "source": "datahub_sync",
                    },
                    occurred_at=occurred_at,
                )
            )
            inserted += 1

        if inserted:
            await self._db.commit()

        return inserted

    async def _observe_passive_operations(self, source_id: str) -> int:
        """Observe Operation timeseries on datasets mapped to a PASSIVE source.

        Best-effort: per-dataset errors are logged and skipped.

        Returns the count of newly inserted events.
        """
        from datahub.metadata.schema_classes import OperationClass

        _INGESTION_OP_TYPES = {"INSERT", "UPDATE", "CREATE", "ALTER"}

        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            return 0

        # Fetch datasets mapped to this PASSIVE source.
        ds_result = await self._db.execute(
            select(IngestionSourceDataset).where(
                IngestionSourceDataset.source_id == uid,
            )
        )
        dataset_rows = ds_result.scalars().all()

        inserted = 0
        for ds_row in dataset_rows:
            try:
                ops = await self._datahub.get_timeseries(
                    ds_row.dataset_urn,
                    OperationClass,
                    limit=5,
                )
            except Exception as exc:
                logger.debug("get_timeseries(Operation) failed for %s: %s", ds_row.dataset_urn, exc)
                continue

            for op in ops:
                op_type = getattr(op, "operationType", None)
                if op_type not in _INGESTION_OP_TYPES:
                    continue

                last_updated_ts = getattr(op, "lastUpdatedTimestamp", None)
                if last_updated_ts:
                    occurred_at = datetime.fromtimestamp(last_updated_ts / 1000, tz=UTC)
                else:
                    occurred_at = datetime.now(tz=UTC)

                event_type = INGESTION_COMPLETE

                # Deduplicate on source, event type, timestamp, and dataset URN.
                dup_result = await self._db.execute(
                    select(Event).where(
                        Event.entity_type == "ingestion_source",
                        Event.entity_id == source_id,
                        Event.event_type == event_type,
                        Event.occurred_at == occurred_at,
                        Event.detail["dataset_urn"].astext == ds_row.dataset_urn,
                    )
                )
                if dup_result.scalar_one_or_none() is not None:
                    continue

                self._db.add(
                    Event(
                        entity_type="ingestion_source",
                        entity_id=source_id,
                        event_type=event_type,
                        status="success",
                        detail={
                            "dataset_urn": ds_row.dataset_urn,
                            "operation_type": op_type,
                            "source": "passive_observation",
                        },
                        occurred_at=occurred_at,
                    )
                )
                inserted += 1
                # One event per dataset per sweep is sufficient.
                break

        if inserted:
            await self._db.commit()

        return inserted
