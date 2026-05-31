"""Ingestion source request/response schemas — per-source model.

The request and response bodies mirror the UC1 recipe YAML 1:1 in JSON.
recipe is the DataHub-compatible {source:{type,config}} object; shape-level
validation is here while semantic validation (secret refs, schedule tier,
platform constraints) is performed by the service layer.

Spec: API.md §Ingestion, §Source body shape
      spec/feature/SECRET_RESOLUTION.md §API schema
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.ingestion import Mode


# ── Sub-models ────────────────────────────────────────────────────────────────


class SecretRefInfo(BaseModel):
    """Metadata for one (secret, key) pair exposed by GET /spoke/ingestion/secrets.

    Values are never included — ref is the literal ``name__key`` string an
    author pastes into a recipe as ``${ref}``.
    """

    ref: str = Field(description="'name__key' token — paste into recipe.source.config as ${ref}")
    secret_name: str = Field(description="Full Kubernetes Secret name: dataspoke-source-cred-<name>")
    key: str = Field(description="Key within the Secret's data map")


# ── Request models ────────────────────────────────────────────────────────────


class CreateIngestionSourceRequest(BaseModel):
    """Request body for POST /spoke/ingestion/sources.

    Only ACTIVE_CUSTOM_MANAGED and PASSIVE are accepted here; DATAHUB_MANAGED
    rows are synced from DataHub (not created by the API).
    """

    mode: Mode = Field(
        description=(
            "Ingestion mode: ACTIVE_CUSTOM_MANAGED (DataSpoke extractor runs on a schedule) "
            "or PASSIVE (ingested outside DataHub/DataSpoke; DataSpoke tracks scope). "
            "DATAHUB_MANAGED is read-only — synced from DataHub."
        ),
    )
    name: str = Field(
        min_length=1,
        max_length=512,
        description="Human-readable name for this source (e.g. 'prod postgres catalog schema')",
    )
    schedule: str | None = Field(
        default=None,
        description=(
            "Cron expression mapping to one of three tiers: "
            "'0 * * * *' (hourly), '0 0 * * *' (daily), '0 0 * * 0' (weekly), "
            "or null for manual-only. Omit (or null) for PASSIVE sources."
        ),
    )
    recipe: dict[str, Any] = Field(
        description=(
            "DataHub-compatible recipe: {source: {type: <str>, config: <dict>}}. "
            "Credentials are referenced as ${name__key} placeholders — never plaintext. "
            "The service validates the shape and verifies all secret refs at save time."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": "dummy postgres example_db in catalog schema",
                "schedule": "0 0 * * *",
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "host_port": "pg.example:5432",
                            "username": "spoke_reader",
                            "password": "${dummy_data_pg__password}",
                            "schema_pattern": {"allow": ["^catalog$"]},
                            "env": "DEV",
                        },
                    }
                },
            }
        }
    }


class ReplaceIngestionSourceRequest(BaseModel):
    """Request body for PUT /spoke/ingestion/sources/{id} (full replacement)."""

    mode: Mode = Field(description="Ingestion mode after the replacement.")
    name: str = Field(min_length=1, max_length=512, description="Display name for this source.")
    schedule: str | None = Field(
        default=None,
        description="Cron expression for the schedule tier, or null for manual-only.",
    )
    recipe: dict[str, Any] = Field(
        description="DataHub-compatible recipe: {source: {type, config}}.",
    )


class PatchIngestionSourceRequest(BaseModel):
    """Request body for PATCH /spoke/ingestion/sources/{id} (partial update).

    All fields are optional. Fields absent from the request body are left unchanged.
    ``mode`` is not patchable — use PUT for a mode change.
    """

    name: str | None = Field(default=None, min_length=1, max_length=512)
    schedule: str | None = Field(
        default=None,
        description=(
            "New cron expression, or null to switch to manual-only. "
            "Explicitly omitting this field leaves the existing schedule unchanged; "
            "pass null to clear it."
        ),
    )
    recipe: dict[str, Any] | None = Field(
        default=None,
        description="Replacement recipe dict. Partial recipe updates are not supported; supply the full new recipe.",
    )


class RunIngestionSourceRequest(BaseModel):
    """Request body for POST /spoke/ingestion/sources/{id}/method/run."""

    dry_run: bool = Field(
        default=False,
        description=(
            "When true, perform a no-write connection and extraction check without "
            "emitting any aspects to DataHub."
        ),
    )


# ── Response models ───────────────────────────────────────────────────────────


class IngestionSourceResponse(SingleResponse):
    """Response shape for GET/POST/PUT/PATCH /spoke/ingestion/sources/{id}.

    recipe is returned verbatim from storage — ``${name__key}`` references
    remain as-is (they are the masked form; plaintext is never stored).
    """

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Unique identifier of the ingestion source (UUID)")
    mode: Mode = Field(description="Ingestion mode: DATAHUB_MANAGED | ACTIVE_CUSTOM_MANAGED | PASSIVE")
    name: str = Field(description="Human-readable name for this source")
    schedule: str | None = Field(
        description="Cron expression for the schedule tier, or null for manual-only"
    )
    recipe: dict[str, Any] = Field(
        description=(
            "DataHub-compatible recipe. Secret values are stored as ${name__key} "
            "references — the response returns those references verbatim (never plaintext)."
        )
    )
    platform: str = Field(description="Derived source platform (recipe.source.type)")
    status: str = Field(description="Source health status: 'OK' or 'ERROR'")
    datahub_source_urn: str | None = Field(
        default=None,
        description="DataHub IngestionSource URN for DATAHUB_MANAGED sources; null otherwise",
    )
    created_at: datetime = Field(description="UTC timestamp when this source was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class IngestionSourceListResponse(PaginatedResponse):
    """Paginated list of ingestion sources."""

    sources: list[IngestionSourceResponse] = Field(
        default=[], description="Page of ingestion source records"
    )


class IngestionRunResponse(SingleResponse):
    """Response for POST /spoke/ingestion/sources/{id}/method/run."""

    run_id: str = Field(description="Run identifier (UUID) generated for this invocation")
    status: str = Field(description="Execution status: 'success' or 'error'")
    detail: dict[str, Any] = Field(
        default={},
        description=(
            "Run metadata: entities_ingested, dry_run, emitted_urns count, "
            "plus optional errors/warnings lists"
        ),
    )


class IngestionSourceDatasetRow(BaseModel):
    """One row in the dataset mapping for a source."""

    dataset_urn: str = Field(description="DataHub URN of the mapped dataset")
    origin: str = Field(
        description="How the mapping was established: 'emitted' | 'pipeline_name' | 'matcher'"
    )
    first_seen_at: datetime = Field(description="UTC timestamp when the mapping was first recorded")
    last_seen_at: datetime = Field(description="UTC timestamp when the mapping was last confirmed")


class IngestionSourceDatasetsResponse(PaginatedResponse):
    """Paginated list of datasets covered by a source."""

    datasets: list[IngestionSourceDatasetRow] = Field(
        default=[], description="Page of dataset mapping rows"
    )


class IngestionUnmanagedResponse(PaginatedResponse):
    """Paginated list of dataset URNs not covered by any source."""

    dataset_urns: list[str] = Field(
        default=[], description="Dataset URNs in the unmanaged bucket"
    )


class SecretRefListResponse(SingleResponse):
    """Response for GET /spoke/ingestion/secrets.

    Lists source-credential references available for use in recipes.
    Values are never returned — only (ref, secret_name, key) metadata.
    """

    secrets: list[SecretRefInfo] = Field(
        default=[],
        description="Available ${name__key} references (one per (secret, key) pair)",
    )


class IngestionLatestRunSummary(BaseModel):
    """Summary of the most recent ingestion run for the owning source."""

    run_id: str | None = Field(
        default=None,
        description="Run identifier, or null when not recorded in the event detail",
    )
    status: str = Field(description="Run outcome: 'success' or 'error'")
    occurred_at: datetime = Field(description="UTC timestamp of the run event")


class IngestionReverseLookupResponse(SingleResponse):
    """Response for GET /spoke/common/data/{dataset_urn}/attr/ingestion.

    Returns the owning source for a dataset, or null if unmapped.
    """

    dataset_urn: str = Field(description="The queried dataset URN")
    source_id: str | None = Field(
        default=None,
        description="ID of the source that covers this dataset, or null if unmapped",
    )
    mode: Mode | None = Field(
        default=None,
        description="Mode of the owning source, or null if unmapped",
    )
    name: str | None = Field(
        default=None,
        description="Name of the owning source, or null if unmapped",
    )
    latest_run: IngestionLatestRunSummary | None = Field(
        default=None,
        description=(
            "Summary of the most recent INGESTION.COMPLETE or INGESTION.FAIL event "
            "for the owning source, or null when no run has been recorded yet."
        ),
    )
