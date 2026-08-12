"""Internal activity endpoints — called by Airflow HTTP operator tasks.

Each endpoint corresponds to an Airflow activity.  Business logic lives in
src/backend/; these endpoints are thin wrappers that translate DataSpokeError
to 400 (non-retryable) or 500 (retryable) HTTP responses, letting Airflow
distinguish between errors worth retrying and permanent failures.

These endpoints are NOT exposed to end users — they are called by the Airflow
orchestrator running inside the same K8s namespace, gated by X-Internal-Token.

Activities:
  /ingestion/list-active  — list source IDs with ACTIVE_CUSTOM_MANAGED configs for a tier
  /ingestion/run          — execute ingestion pipeline for a single source
  /ingestion/sync         — reconcile all ingestion sources + dataset_registry against DataHub
  /metagen/run            — run every enabled metagen conf matching the fired tier
  /metrics/list-active    — list metric IDs with is_enabled=True for a tier
  /metrics/run            — execute metric measurement for a single metric
  /ontogen/run            — execute the ontogen inference pipeline (singleton)
  /auth/role-sync         — reconcile DataHub-side role assignments against users.role

Spec: spec/feature/BACKEND.md §DAG Catalogue + §Dependency Injection.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.auth.internal import require_internal_token
from src.shared.events import AUTH_ROLE_SYNC_FIXED
from src.shared.exceptions import ConflictError, DataSpokeError
from src.shared.models.enums import EventStatus
from src.workflows._common import (
    make_datahub,
    make_db_session,
    make_llm_client,
    make_pgvector_manager,
    make_redis_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/activities",
    tags=[
        "internal/activities/ingestion",
        "internal/activities/metagen",
        "internal/activities/metrics",
        "internal/activities/ontogen",
        "internal/activities/auth",
    ],
    dependencies=[Depends(require_internal_token)],
)


def _error_response(exc: Exception, non_retryable: bool = True) -> JSONResponse:
    """Map DataSpokeError → 400 (non-retryable) or 500 (retryable)."""
    error_code = getattr(exc, "error_code", "INTERNAL_ERROR")
    status_code = 400 if non_retryable else 500
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": str(exc), "non_retryable": non_retryable},
    )


# ── /ingestion ────────────────────────────────────────────────────────────────


class IngestionListActiveRequest(BaseModel):
    tier: str  # "hourly" | "daily" | "weekly"


@router.post("/ingestion/list-active")
async def ingestion_list_active(body: IngestionListActiveRequest) -> list[str]:
    """Return source IDs with ACTIVE_CUSTOM_MANAGED ingestion configs for the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db)
            records = await service.list_active_sources_for_tier(body.tier)
            return [r.id for r in records]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


class IngestionRunRequest(BaseModel):
    source_id: str
    dry_run: bool = False


@router.post("/ingestion/run")
async def ingestion_run(body: IngestionRunRequest) -> dict[str, object]:
    """Execute ingestion pipeline for a single ACTIVE_CUSTOM_MANAGED source."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.ingestion.service import IngestionService, run_report_detail

            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db, cache=cache)
            result = await service.run(source_id=body.source_id, dry_run=body.dry_run)
            return {
                "run_id": result.run_id,
                "status": result.status,
                "dry_run": result.dry_run,
                **run_report_detail(result),
                "errors": result.errors,
                "warnings": result.warnings,
            }
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


@router.post("/ingestion/sync")
async def ingestion_sync() -> dict[str, object]:
    """Reconcile all ingestion sources against DataHub.

    Called hourly by the datahub-sync-hourly DAG. Runs the sync pipeline
    (source defs, mapping, dataset_registry reconcile, observed enrichment,
    run and observation events) and returns a summary dict.

    Retryable: DataHub transient failures surface as 500 so Airflow retries.

    Returns:
        {sources_synced, sources_removed, datasets_mapped, pipeline_links,
         events_mirrored, last_ingested_observed, registry_inserted,
         registry_marked_true, registry_marked_false, sources_zero_coverage,
         sources_pattern_degraded}

        ``last_ingested_observed`` counts the per-dataset INGESTION.COMPLETE events
        booked from the estate-wide Dataset.lastIngested read. The first sweep of a
        fresh deployment books the whole observable backlog, so a large first
        reading is historical catch-up rather than a run storm.

        ``sources_zero_coverage`` and ``sources_pattern_degraded`` are the two
        defect signals: a non-zero ``sources_pattern_degraded`` means that many
        sources had no usable pattern set this sweep, so their stored mappings were
        left unreconciled rather than rebuilt. Every other counter can read as a
        healthy no-op sweep while that is true.
    """
    try:
        async with make_db_session() as db:
            from src.backend.ingestion.service import IngestionService

            datahub = await make_datahub(db)
            service = IngestionService(datahub=datahub, db=db)
            summary = await service.sync()
            return summary
    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]


# ── /metagen ──────────────────────────────────────────────────────────────────


class MetagenRunRequest(BaseModel):
    # Internal variant; public counterpart is src/api/schemas/metagen.MetagenRunRequest (no `tier`).
    tier: str | None = None
    dataset_urns: list[str] | None = None
    dry_run: bool = False


@router.post("/metagen/run")
async def metagen_run(body: MetagenRunRequest) -> dict[str, object]:
    """Execute the metagen pipeline across every enabled conf matching ``tier``.

    Called by the three metagen tier DAGs. Each tier DAG supplies ``tier``; the
    activity enumerates all ``is_enabled=true`` confs whose ``schedule_tier``
    matches and runs each one under its own per-conf lock
    (``metagen:running:{conf_id}``). A conf already in flight (its lock held) is
    skipped for this tick. Per-conf results are aggregated in the response.

    Spec: feature/BACKEND.md §Scheduled fan-out.
    """
    from src.backend.admin.config_service import get_runtime_config
    from src.backend.metagen.service import MetagenService
    from src.shared.exceptions import ConflictError

    async with make_db_session() as db:
        datahub = await make_datahub(db)
        rc = await get_runtime_config(db)
        cache = make_redis_client(stub=rc.stub_redis_client)
        vector = make_pgvector_manager(stub=rc.stub_pgvector_manager)
        llm = make_llm_client(
            stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model
        )
        service = MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)

        # Enumerate enabled confs matching the requested tier.
        confs, _ = await service.list_confs(offset=0, limit=1000)
        targets = [
            c
            for c in confs
            if c.is_enabled and (body.tier is None or c.schedule_tier == body.tier)
        ]

        results: list[dict[str, object]] = []
        for conf in targets:
            try:
                result = await service.run(
                    conf.id,
                    dataset_urns=body.dataset_urns,
                    dry_run=body.dry_run,
                )
                results.append({"conf_id": conf.id, "status": "completed", **result.model_dump()})
            except ConflictError as exc:
                # A conf already in flight (lock held) is skipped for this tick.
                if getattr(exc, "error_code", None) == "METAGEN_RUNNING":
                    results.append(
                        {"conf_id": conf.id, "status": "skipped", "reason": "already_running"}
                    )
                else:
                    results.append(
                        {
                            "conf_id": conf.id,
                            "status": "failed",
                            "error_code": getattr(exc, "error_code", None),
                        }
                    )
            except DataSpokeError as exc:
                results.append(
                    {
                        "conf_id": conf.id,
                        "status": "failed",
                        "error_code": getattr(exc, "error_code", None),
                    }
                )
            except Exception as exc:
                # Aggregate the failure without aborting the rest of the tier
                # (BACKEND.md §Scheduled fan-out). The service has already
                # recorded RUN_FAILED for this conf before re-raising.
                results.append(
                    {
                        "conf_id": conf.id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        return {
            "status": "completed",
            "tier": body.tier,
            "conf_count": len(targets),
            "results": results,
        }


# ── /metrics ──────────────────────────────────────────────────────────────────


class MetricsListActiveRequest(BaseModel):
    tier: str


@router.post("/metrics/list-active")
async def metrics_list_active(body: MetricsListActiveRequest) -> list[str]:
    """Return metric IDs with is_enabled=True and schedule_tier matching the given tier."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.metrics.service import MetricsService

            rc = await get_runtime_config(db)
            datahub = await make_datahub(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            records = await service.list_active_for_tier(body.tier)
            return [r.id for r in records]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


_METRIC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$"


class MetricsRunRequest(BaseModel):
    metric_id: str = Field(
        pattern=_METRIC_ID_PATTERN,
        max_length=64,
    )
    dry_run: bool = False


@router.post("/metrics/run")
async def metrics_run(body: MetricsRunRequest) -> dict[str, object]:
    """Execute metric measurement run for a single metric."""
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.metrics.service import MetricsService

            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            datahub = await make_datahub(db)
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            result = await service.run(body.metric_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except ConflictError as exc:
        if exc.error_code == "METRIC_RUNNING":
            # Return HTTP 200 with structured error payload so the DAG task stays
            # green; the calling route inspects dag_run.conf to translate to 409.
            return {
                "run_id": "",
                "status": "error",
                "detail": {"error_code": "METRIC_RUNNING", "message": str(exc)},
            }
        return _error_response(exc)  # type: ignore[return-value]
    except DataSpokeError as exc:
        return _error_response(exc)  # type: ignore[return-value]


# ── /ontogen ──────────────────────────────────────────────────────────────────


class OntogenRunRequest(BaseModel):
    dry_run: bool = False
    prompt_md: str | None = None
    tier: str | None = None  # set by periodic tier DAGs; None for on-demand DAG


@router.post("/ontogen/run")
async def ontogen_run(body: OntogenRunRequest) -> dict[str, object]:
    """Execute the ontogen inference pipeline.

    Called by the three ontogen tier DAGs. Each tier DAG supplies ``tier``;
    the activity short-circuits when ``tier`` does not match
    ``ontogen_config.schedule_tier`` so only the one DAG matching the conf
    actually runs. Manual API calls (``POST /spoke/ontogen/method/run``)
    call OntogenService.run() directly in-process.

    Spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection.
    """
    try:
        async with make_db_session() as db:
            from src.backend.admin.config_service import get_runtime_config
            from src.backend.ontogen.service import OntogenService

            datahub = await make_datahub(db)
            rc = await get_runtime_config(db)
            cache = make_redis_client(stub=rc.stub_redis_client)
            vector = make_pgvector_manager(stub=rc.stub_pgvector_manager)
            llm = make_llm_client(
                stub=rc.stub_llm_client, provider=rc.llm_provider, model=rc.llm_model
            )
            service = OntogenService(
                datahub=datahub,
                db=db,
                cache=cache,
                llm=llm,
                vector=vector,
            )

            if body.tier is not None:
                conf = await service.get_conf()
                if body.tier != conf.schedule_tier:
                    return {
                        "status": "skipped",
                        "reason": "tier_mismatch",
                        "dag_tier": body.tier,
                        "conf_tier": conf.schedule_tier,
                    }
                if not conf.is_enabled:
                    return {"status": "skipped", "reason": "disabled"}

            summary = await service.run(prompt_md=body.prompt_md, dry_run=body.dry_run)
            return {
                "status": summary.status,
                "dry_run": summary.dry_run,
                "unresolved_urns": summary.unresolved_urns,
                "counts": summary.counts,
            }
    except DataSpokeError as exc:
        # ONTOGEN_RUNNING (409) → retryable = True (Airflow will retry)
        non_retryable = exc.error_code != "ONTOGEN_RUNNING" if hasattr(exc, "error_code") else True
        return _error_response(exc, non_retryable=non_retryable)  # type: ignore[return-value]


# ── /auth ─────────────────────────────────────────────────────────────────────


@router.post("/auth/role-sync")
async def auth_role_sync() -> dict[str, object]:
    """Reconcile the DataHub-side projection against DataSpoke users (SSOT).

    Called daily by the auth-role-sync-daily DAG.

    Two facets are projected onto each corpuser: its role and its membership in
    the marker corpGroup. DataSpoke owns both, so any DataHub-side divergence is
    corrected here — DataSpoke wins.

    Algorithm:
      1. Ensure the marker corpGroup exists — ONCE, before the loop.
         ``ensure_marker_group_exists`` resets CorpGroupInfo.members=[] on every
         call, and addGroupMembers rejects an unresolvable group, so the group
         must be ensured exactly once and before any membership write.
      2. SELECT all rows from users ordered by id.
         (Baseline uses a simple full-scan; for large deployments the optimisation
         path is scrollAcrossEntities on the marker corpGroup instead.)
      3. Skip rows with no verified Google identity (google_sub IS NULL). The
         corpuser URN is derived from users.email, and a password-registered
         email is unverified — projecting for an unbound row would let someone
         who registered another person's address steer that person's DataHub
         role. Counted as skipped_unbound.
      4. Probe each user's corpuser for existence BEFORE any mutation. DataHub's
         RoleService returns early when the actor does not exist while the
         GraphQL mutation still reports success, so an unguarded pass would count
         repairs it never made. Users whose corpuser DataHub has not yet
         JIT-provisioned (nobody has signed into DataHub as them) are counted as
         skipped_unprovisioned and left alone.
      5. Role facet — read the RoleMembership aspect (atomic single-role per
         DataHub RoleService); on divergence from users.role, re-assert via
         batchAssignRole.
      6. Group facet — read the nativeGroupMembership aspect; when the marker
         group URN is absent, add it via addGroupMembers. Attempted independently
         of the role facet: the two are unrelated, so a role-facet failure must
         not suppress a group repair.
      7. Emit one AUTH.ROLE_SYNC_FIXED event per repaired user, whose detail
         names the repaired facet(s).

    Only rows in the DataSpoke users table are in scope — DataHub-only corpusers
    are out of scope, and deleted users are invisible to this pass (retraction is
    handled at delete time by DELETE /admin/users/{id}).

    Counter semantics — the buckets are NOT a partition of ``checked``:
      - ``checked``  — users examined.
      - ``fixed``    — users with at least one facet repaired (at most one per
        user, never per facet). The per-facet breakdown is in the event detail.
      - ``skipped_unbound`` / ``skipped_unprovisioned`` — mutually exclusive,
        and disjoint from the rest; neither is probed or mutated further.
      - ``errors``   — users for whom at least one facet could not be
        reconciled. This **may overlap with ``fixed``**: when the role facet is
        repaired and the group facet then fails, the user counts in both.

    Failure handling: a per-facet failure is logged, counted in ``errors``, and
    the pass continues to the next facet or user. All exception types are caught,
    not just DataHubUnavailableError — the SDK raises GraphError for an HTTP 200
    body carrying an ``errors`` array (and on a 401/403 from a rotated PAT),
    which is neither a DataSpokeError nor a transport error.

    Accumulated event rows are committed even when the pass aborts partway. The
    DataHub mutations behind them have already landed and a retry sees no drift,
    so dropping the rows would erase the audit trail permanently.

    A failure of the one-time step 1 aborts the pass rather than degrading it:
    without a resolvable marker group the group facet cannot be repaired for
    anyone, and a pass that continued would silently under-report drift. The
    outer handler returns a retryable response so Airflow retries the run.

    An unconfigured DataHub peripheral returns a zero-count no-op rather than an
    error: running before DataHub is wired is a supported steady state.

    Returns {checked, fixed, skipped_unprovisioned, skipped_unbound, errors}.

    Spec: spec/feature/AUTH.md §Role Drift Reconciliation,
          spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation.
    """
    from sqlalchemy import select

    from src.backend.admin.config_service import get_runtime_config
    from src.backend.datahub import users as dh_users
    from src.shared.db.models import Event, User
    from src.shared.exceptions import PeripheralNotConfiguredError

    checked = 0
    fixed = 0
    skipped_unprovisioned = 0
    skipped_unbound = 0
    errors = 0
    event_rows: list[Event] = []

    def _result() -> dict[str, object]:
        return {
            "checked": checked,
            "fixed": fixed,
            "skipped_unprovisioned": skipped_unprovisioned,
            "skipped_unbound": skipped_unbound,
            "errors": errors,
        }

    try:
        async with make_db_session() as db:
            try:
                datahub = await make_datahub(db)
            except PeripheralNotConfiguredError:
                logger.info("auth_role_sync: DataHub peripheral unconfigured; nothing to project")
                return _result()

            runtime_config = await get_runtime_config(db)
            group_name = runtime_config.auth_datahub_corp_group
            group_urn = dh_users.corpgroup_urn(group_name)

            try:
                await dh_users.ensure_marker_group_exists(datahub, group_name)

                result = await db.execute(select(User).order_by(User.id))
                users = result.scalars().all()

                for user in users:
                    checked += 1

                    if user.google_sub is None:
                        skipped_unbound += 1
                        continue

                    urn = dh_users.corpuser_urn(user.email)
                    log_extra = {"user_id": str(user.id), "corpuser_urn": urn}

                    try:
                        if not await dh_users.corpuser_exists(datahub, urn):
                            skipped_unprovisioned += 1
                            continue
                    except Exception:
                        logger.warning(
                            "auth_role_sync: probe failed for corpuser",
                            extra=log_extra,
                            exc_info=True,
                        )
                        errors += 1
                        continue

                    repaired_facets: list[str] = []
                    detail: dict[str, object] = {}
                    facet_failed = False

                    # ── Role facet ────────────────────────────────────────────
                    try:
                        observed_role = await dh_users.read_role(datahub, urn)
                        if observed_role != user.role:
                            await dh_users.propagate_role(datahub, urn, user.role)
                            repaired_facets.append("role")
                            detail["dataspoke_role_authoritative"] = user.role
                            detail["datahub_role_observed"] = observed_role
                    except Exception:
                        logger.warning(
                            "auth_role_sync: role facet failed",
                            extra=log_extra,
                            exc_info=True,
                        )
                        facet_failed = True

                    # ── Marker-group facet ────────────────────────────────────
                    # Attempted regardless of the role facet's outcome — the two
                    # facets are independent.
                    try:
                        membership = await dh_users.read_native_group_membership(datahub, urn)
                        if group_urn not in membership:
                            await dh_users.add_user_to_marker_group(datahub, group_urn, urn)
                            repaired_facets.append("group")
                            detail["marker_group_urn"] = group_urn
                    except Exception:
                        logger.warning(
                            "auth_role_sync: group facet failed",
                            extra=log_extra,
                            exc_info=True,
                        )
                        facet_failed = True

                    if facet_failed:
                        errors += 1

                    if repaired_facets:
                        fixed += 1
                        detail["repaired_facets"] = repaired_facets
                        event_rows.append(
                            Event(
                                entity_type="user",
                                entity_id=str(user.id),
                                event_type=AUTH_ROLE_SYNC_FIXED,
                                status=EventStatus.SUCCESS,
                                detail=detail,
                            )
                        )
            finally:
                # Persist the audit rows for repairs that already landed in
                # DataHub, even when the pass aborts partway. A retry observes no
                # drift and would emit nothing, so these rows are unrecoverable.
                if event_rows:
                    db.add_all(event_rows)
                await db.commit()

    except DataSpokeError as exc:
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]
    except Exception as exc:
        # The SDK raises GraphError (not a DataSpokeError) for an HTTP 200 body
        # carrying an errors array, and on a 401/403 from a rotated PAT. Retryable
        # so Airflow re-runs; the accumulated event rows were committed above.
        logger.warning("auth_role_sync: pass aborted", exc_info=True)
        return _error_response(exc, non_retryable=False)  # type: ignore[return-value]

    return _result()
