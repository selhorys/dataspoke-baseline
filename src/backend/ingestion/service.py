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
from datetime import UTC, datetime
from typing import Any

from datahub.metadata.schema_classes import (  # type: ignore
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
    build_matcher,
    cron_to_tier,
    extract_secret_refs,
    parse_recipe,
)
from src.shared.secrets import (
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
    resolve_recipe_secrets,
    verify_secret_ref,
)

logger = logging.getLogger(__name__)

# DataHub execution-result status mapping:
#   SUCCESS (and SUCCEEDED, for cross-version safety) → INGESTION_COMPLETE
#   SKIPPED / UP_FOR_RETRY → skipped (not mirrored as events)
#   every other terminal status (FAILURE/CANCELLED/ABORTED/TIMEOUT/…) → INGESTION_FAIL
_DATAHUB_SUCCESS_STATUSES: frozenset[str] = frozenset({"SUCCESS", "SUCCEEDED"})
# Non-terminal / ambiguous DataHub statuses — not real outcomes, so they are
# not mirrored as events (UP_FOR_RETRY may still succeed; SKIPPED ran nothing).
_DATAHUB_SKIP_STATUSES: frozenset[str] = frozenset({"SKIPPED", "UP_FOR_RETRY"})


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
    ad_hoc: bool
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionRunResult(BaseModel):
    """Value object for the outcome of an ingestion run."""

    run_id: str
    status: str  # 'success' | 'error'
    dry_run: bool
    entities_ingested: int
    emitted_urns: list[str]
    errors: list[str]
    warnings: list[str]


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
        ad_hoc=row.ad_hoc,
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_ad_hoc(executor_id: str | None, source_urn: str | None, name: str | None) -> bool:
    """Classify a DATAHUB_MANAGED source as CLI/ad-hoc.

    A source is ad-hoc when ANY of the following holds (priority order):
      1. ``executor_id`` starts with ``__datahub_cli_`` (primary, decisive)
      2. the source URN id starts with ``cli-`` — the id is the last
         colon-segment of ``urn:li:dataHubIngestionSource:<id>``
      3. the display name starts with ``[CLI] ``

    ``pipeline_name`` is not a marker. Non-DATAHUB_MANAGED sources are never
    ad-hoc and should not be passed here.
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


def _parse_cli_parent_urn(name: str | None) -> str | None:
    """Extract the parent registered-source URN from a ``[CLI]`` wrapper name.

    DataHub auto-creates an ad-hoc CLI wrapper source when ``datahub ingest``
    runs a recipe carrying a ``pipeline_name``, with display name
    ``[CLI] <type> [<pipeline_name>]`` — the ``pipeline_name`` embedded verbatim
    in the trailing brackets. When DataSpoke runs a registered source, that
    ``pipeline_name`` is the parent registered-source URN.

    Returns the bracketed value when it is a ``urn:li:dataHubIngestionSource:…``
    URN; returns None when ``name`` is absent, is not a ``[CLI] `` name, has no
    trailing bracket, or the bracketed content is not a dataHubIngestionSource
    URN. Pure string parse — no DB or network access.
    """
    if not name or not name.startswith("[CLI] "):
        return None
    if not name.endswith("]"):
        return None
    open_idx = name.rfind("[")
    if open_idx == -1:
        return None
    inner = name[open_idx + 1 : -1]
    if not inner.startswith("urn:li:dataHubIngestionSource:"):
        return None
    return inner


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
        ad_hoc: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[IngestionSourceRecord], int]:
        """Return a paginated list of sources.

        Args:
            mode_filter: When provided, filter to sources with this mode value.
            ad_hoc: Tri-state filter on the derived CLI/ad-hoc classification.
                ``None`` applies no constraint; ``True``/``False`` filter to
                that exact classification.
        """
        base = select(IngestionSource)
        if mode_filter is not None:
            base = base.where(IngestionSource.mode == mode_filter)
        if ad_hoc is not None:
            base = base.where(IngestionSource.ad_hoc == ad_hoc)

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
            ad_hoc=False,
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
                entities_ingested=0,
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
                entities_ingested=0,
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
            ingestion_result = await run_extractor(
                datahub=self._datahub,
                source_id=source_id,
                recipe=resolved_recipe,
                dry_run=dry_run,
                run_id=run_id,
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

        # A non-dry-run that ingested zero entities is treated as failure.
        if not dry_run and ingestion_result.entities_ingested == 0 and not errors:
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

        event_type = INGESTION_COMPLETE if status == "success" else INGESTION_FAIL
        await self._record_source_event(
            source_id,
            event_type,
            status,
            {
                "run_id": run_id,
                "platform": source.platform,
                "entities_ingested": ingestion_result.entities_ingested,
                "dry_run": dry_run,
                "errors": errors,
                "warnings": warnings,
            },
        )

        return IngestionRunResult(
            run_id=run_id,
            status=status,
            dry_run=dry_run,
            entities_ingested=ingestion_result.entities_ingested,
            emitted_urns=ingestion_result.emitted_urns,
            errors=errors,
            warnings=warnings,
        )

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
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"dataspoke-{source.platform}-{run_id}",
                type=run_type,
                created=AuditStampClass(time=start_ms, actor="urn:li:corpuser:dataspoke"),
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
        When multiple sources map the same dataset at the same priority, returns
        the one with the most recent ``last_seen_at``.

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

        _DERIVATION_PRIORITY = {"emitted": 0, "pipeline_name": 1, "matched": 2}

        def _key(pair: tuple[IngestionSourceDataset, IngestionSource]) -> tuple[int, datetime]:
            mapping, _ = pair
            priority = _DERIVATION_PRIORITY.get(mapping.derivation, 99)
            # Negate last_seen_at so that most recent sorts first within the same priority.
            return (priority, -mapping.last_seen_at.timestamp())

        best_mapping, best_source = sorted(rows, key=_key)[0]
        return _source_from_row(best_source)

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
        ``entity_id=source_id``.
        """
        base = select(Event).where(
            Event.entity_type == "ingestion_source",
            Event.entity_id == source_id,
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
           runs). Reconcile dataset_registry to mirror the full DataHub URN set:
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
            Summary dict for the activity endpoint / logging:
              sources_synced    — DATAHUB_MANAGED rows upserted
              sources_removed   — DATAHUB_MANAGED rows removed (gone from DataHub)
              datasets_mapped   — new ingestion_source_dataset matched rows inserted
              pipeline_links    — pipeline_name-derivation rows upserted
              events_mirrored   — new INGESTION events written
              registry_inserted — new dataset_registry rows inserted (datahub_registered=True)
              registry_marked_true   — existing rows flipped from False to True
              registry_marked_false  — existing rows soft-flagged as False (left DataHub)
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
        }

        # ── Step 1: Source defs (DATAHUB_MANAGED) ────────────────────────────
        dh_sources = await self._datahub.list_ingestion_sources()
        seen_urns: set[str] = set()

        for s in dh_sources:
            source_urn = s.get("urn") or ""
            if not source_urn:
                continue
            seen_urns.add(source_urn)

            recipe_str = s.get("recipe") or ""
            recipe_dict = _parse_recipe_str_safe(recipe_str)
            masked_recipe = _mask_recipe_secrets(recipe_dict)

            source_type, _ = _safe_parse_recipe(masked_recipe)
            schedule_interval: str | None = (s.get("schedule") or {}).get("interval") or None

            # Derive internal tier for informational purposes only — no error on unknown.
            try:
                tier = cron_to_tier(schedule_interval)
            except ValueError:
                tier = None

            # Upsert the DATAHUB_MANAGED row matched on datahub_source_urn.
            result = await self._db.execute(
                select(IngestionSource).where(
                    IngestionSource.datahub_source_urn == source_urn,
                    IngestionSource.mode == Mode.DATAHUB_MANAGED.value,
                )
            )
            row = result.scalar_one_or_none()
            now = datetime.now(tz=UTC)

            ad_hoc = _is_ad_hoc(s.get("executor_id"), source_urn, s.get("name"))

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
                    ad_hoc=ad_hoc,
                    status="OK",
                )
                self._db.add(row)
            else:
                row.name = s.get("name") or source_urn
                row.platform = source_type
                row.recipe = masked_recipe
                row.schedule = schedule_interval
                row.schedule_tier = tier
                row.ad_hoc = ad_hoc
                row.status = "OK"
                row.updated_at = now
                self._db.add(row)

            summary["sources_synced"] += 1

        await self._db.commit()

        # Remove DATAHUB_MANAGED rows whose source URN is no longer in DataHub.
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
            matcher = build_matcher(recipe)
            matched_urns: set[str] = {
                urn
                for urn, name in urn_to_name.items()
                if urn_to_platform.get(urn) == expected_platform and matcher(name)
            }

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
                if insert_result.rowcount == 1 and urn not in existing_matcher_rows:
                    summary["datasets_mapped"] += 1

            # Prune stale matched rows (derivation='matched') that no longer match.
            # These rows were fetched with derivation=='matched' filter above, so this
            # loop can only delete matched-derivation rows — emitted/pipeline_name rows
            # are never in existing_matcher_rows and are therefore never deleted here.
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
            # a registered source and the ad-hoc CLI wrapper that inherits from it).
            # DATAHUB_MANAGED registered: pipelineName == datahub_source_urn (DataHub
            #   stamps the registered source URN on emitted aspects).
            # DATAHUB_MANAGED ad-hoc CLI wrapper: a `datahub ingest` run of a
            #   registered source stamps aspects with pipelineName = parent registered
            #   URN, and the wrapper's display name embeds that parent URN as
            #   `[CLI] <type> [<parent_urn>]`. The wrapper inherits the link when its
            #   parsed parent URN matches a registered DATAHUB_MANAGED source row.
            # ACTIVE_CUSTOM_MANAGED: pipelineName == str(source.id) (DataSpoke extractor
            #   stamps pipelineName = source_id per the DPI emission convention).
            registered_managed_urns = {
                row.datahub_source_urn
                for row in all_sources_rows
                if row.mode == Mode.DATAHUB_MANAGED.value
                and not row.ad_hoc
                and row.datahub_source_urn
            }

            pipeline_to_sources: dict[str, list[uuid.UUID]] = {}
            for src_row in all_sources_rows:
                if src_row.mode == Mode.DATAHUB_MANAGED.value:
                    if not src_row.ad_hoc:
                        if src_row.datahub_source_urn:
                            pipeline_to_sources.setdefault(src_row.datahub_source_urn, []).append(
                                src_row.id
                            )
                    else:
                        # Ad-hoc CLI wrapper: inherit only when the parsed parent URN
                        # resolves to a registered DATAHUB_MANAGED source row. Fallback
                        # (unparseable name or no matching registered row): inherit
                        # nothing — keep the step-2 matched/medium mapping.
                        parent_urn = _parse_cli_parent_urn(src_row.name)
                        if parent_urn and parent_urn in registered_managed_urns:
                            pipeline_to_sources.setdefault(parent_urn, []).append(src_row.id)
                elif src_row.mode == Mode.ACTIVE_CUSTOM_MANAGED.value:
                    pipeline_to_sources.setdefault(str(src_row.id), []).append(src_row.id)

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
                    # slot). Count only rows actually written, not upsert attempts.
                    if insert_result.rowcount == 1:
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
        """Mirror terminal execution requests for a DATAHUB_MANAGED source.

        Deduplicates by (entity_id, event_type, occurred_at).

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
            if status_str in _DATAHUB_SKIP_STATUSES:
                continue
            event_type = (
                INGESTION_COMPLETE if status_str in _DATAHUB_SUCCESS_STATUSES else INGESTION_FAIL
            )
            start_ms = req.get("startTimeMs")
            if start_ms:
                occurred_at = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
            else:
                occurred_at = datetime.now(tz=UTC)

            # Deduplicate: skip if (entity_id, event_type, occurred_at) exists.
            dup_result = await self._db.execute(
                select(Event).where(
                    Event.entity_type == "ingestion_source",
                    Event.entity_id == source_id,
                    Event.event_type == event_type,
                    Event.occurred_at == occurred_at,
                )
            )
            if dup_result.scalar_one_or_none() is not None:
                continue

            self._db.add(
                Event(
                    entity_type="ingestion_source",
                    entity_id=source_id,
                    event_type=event_type,
                    status="success" if event_type == INGESTION_COMPLETE else "failure",
                    detail={
                        "execution_request_urn": req.get("urn") or "",
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
        from datahub.metadata.schema_classes import OperationClass  # type: ignore

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

                # Deduplicate.
                dup_result = await self._db.execute(
                    select(Event).where(
                        Event.entity_type == "ingestion_source",
                        Event.entity_id == source_id,
                        Event.event_type == event_type,
                        Event.occurred_at == occurred_at,
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
