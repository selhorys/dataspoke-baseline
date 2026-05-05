"""Ingestion config CRUD and run request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.backend.ingestion.secret_resolver import _NAME_PREFIX
from src.shared.models.enums import IngestionConfigStatus
from src.shared.models.ingestion import (
    Platform,
    validate_platform_fields,
)

_VALID_TIERS = frozenset({"hourly", "daily", "weekly"})
_VALID_MODES = frozenset({"active-custom", "passive"})


# ── Auth sub-models ───────────────────────────────────────────────────────────


class SecretRefSpec(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=253,
        description="Kubernetes Secret name (k8s DNS-label limit: 253 chars)",
    )
    key: str = Field(min_length=1, description="Key within the Kubernetes Secret data map")
    force_overwrite: bool = Field(
        default=False,
        description=(
            "Vault path only: when true, overwrite an existing (name, key) "
            "instead of raising 422 SecretCollision. Ignored on reference path."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name_prefix(cls, v: str) -> str:
        if not v.startswith(_NAME_PREFIX):
            raise ValueError(
                f"SecretRefNameForbidden: secret_ref.name must start with '{_NAME_PREFIX}'; "
                f"got '{v}'"
            )
        return v


class AuthSpec(BaseModel):
    username: str = Field(min_length=1, description="Database / service username")
    password: str | None = Field(
        default=None,
        description=(
            "Plaintext password — vault path only. Written to the Kubernetes Secret "
            "identified by secret_ref and never persisted in the DataSpoke database."
        ),
    )
    secret_ref: SecretRefSpec | None = Field(
        default=None,
        description=(
            "Reference to a Kubernetes Secret in DataSpoke's own namespace. "
            "Required when password is present (vault path) or alone (reference path)."
        ),
    )

    @model_validator(mode="after")
    def enforce_matrix(self) -> "AuthSpec":
        if self.password is None and self.secret_ref is None:
            raise ValueError(
                "auth must include secret_ref (reference path) or password+secret_ref (vault path)"
            )
        if self.password is not None and self.secret_ref is None:
            raise ValueError(
                "Plaintext-only auth is not allowed; supply secret_ref (vault path) "
                "or omit password and reference a pre-existing secret"
            )
        return self


# ── Request models ────────────────────────────────────────────────────────────


class CreateIngestionConfigRequest(BaseModel):
    mode: Literal["active-custom", "passive"] = Field(
        default="active-custom",
        description=(
            "Ingestion mode: 'active-custom' (DataSpoke's in-house extractor runs on "
            "schedule_tier, handling connectivity and auth) or 'passive' (external "
            "pipeline or DataHub Managed Ingestion handles connectivity/auth out-of-band; "
            "DataSpoke mirrors run history)."
        ),
    )
    platform: Platform = Field(
        description="Data platform that determines the locator/identifier/auth structure"
    )
    locator: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Infrastructure location. Required for `active-custom` mode; must be omitted "
            "for `passive` mode (passive ingestors handle their own connectivity out-of-band). "
            "Structure varies by platform:\n"
            "- postgres/mysql/oracle: {\"host\": \"db.example.com\", \"port\": 5432}\n"
            "- bigquery: {\"project_id\": \"my-project\"}\n"
            "- snowflake: {\"account_id\": \"abc12345\"}\n"
            "- kafka: {\"bootstrap_servers\": \"kafka:9092\"}"
        ),
    )
    identifier: dict[str, Any] = Field(
        description=(
            "Dataset identity within the source infrastructure. Structure varies by platform:\n"
            "- postgres/mysql/oracle: {\"database\": \"mydb\", \"schema_name\": \"public\", \"table\": \"orders\"}\n"
            "- bigquery: {\"dataset\": \"analytics\", \"table\": \"events\"}\n"
            "- snowflake: {\"database\": \"DW\", \"schema_name\": \"PUBLIC\", \"table\": \"SALES\"}\n"
            "- kafka: {\"topic\": \"user-events\", \"cluster\": \"prod\"}"
        )
    )
    auth: AuthSpec | None = Field(
        default=None,
        description=(
            "Access credentials. Required for postgres/mysql/oracle/snowflake; "
            "omit for bigquery/kafka. "
            "Vault path: supply both password and secret_ref. "
            "Reference path: supply secret_ref only (pre-provisioned Secret)."
        ),
    )
    is_enabled: bool = Field(
        default=False,
        description="Whether the ingestion config is enabled and scheduled to run (active-custom mode only)",
    )
    schedule_tier: str | None = Field(
        default=None,
        description=(
            "Schedule tier for periodic active-custom-mode runs: 'hourly', 'daily', or 'weekly'. "
            "Required when mode is 'active-custom' and is_enabled is true; "
            "must not be set for passive mode."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "mode": "active-custom",
                "platform": "postgres",
                "locator": {"host": "db.example.com", "port": 5432},
                "identifier": {"database": "mydb", "schema_name": "public", "table": "orders"},
                "auth": {
                    "username": "readonly",
                    "secret_ref": {"name": "dataspoke-source-cred-mydb-creds", "key": "password"},
                },
                "is_enabled": True,
                "schedule_tier": "daily",
            }
        }
    }

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_fields(self) -> "CreateIngestionConfigRequest":
        if self.mode == "passive":
            if self.schedule_tier is not None:
                raise ValueError("schedule_tier is not allowed for passive mode")
            if self.locator is not None:
                raise ValueError("locator is not allowed for passive mode")
            if self.auth is not None:
                raise ValueError("auth is not allowed for passive mode")
            return self
        if self.locator is None:
            raise ValueError("locator is required for active-custom mode")
        if self.is_enabled and not self.schedule_tier:
            raise ValueError(
                "schedule_tier is required when is_enabled is true and mode is active-custom"
            )
        auth_dict: dict[str, Any] | None = None
        if self.auth is not None:
            auth_dict = {"username": self.auth.username}
            if self.auth.secret_ref is not None:
                auth_dict["secret_ref"] = {
                    "name": self.auth.secret_ref.name,
                    "key": self.auth.secret_ref.key,
                }
        validate_platform_fields(
            self.platform, self.locator, self.identifier, auth_dict
        )
        return self


class PatchIngestionConfigRequest(BaseModel):
    mode: Literal["active-custom", "passive"] | None = Field(
        default=None, description="Update the ingestion mode ('active-custom' or 'passive')"
    )
    platform: Platform | None = Field(
        default=None,
        description="Update the data platform (also re-validates locator/identifier/auth when provided)",
    )
    locator: dict[str, Any] | None = Field(
        default=None,
        description="Updated infrastructure location dict.",
    )
    identifier: dict[str, Any] | None = Field(
        default=None,
        description="Updated dataset identity dict.",
    )
    auth: AuthSpec | None = Field(
        default=None, description="Updated access credentials. Same vault/reference shapes as PUT."
    )
    is_enabled: bool | None = Field(
        default=None,
        description="Set to true to activate scheduling (schedule_tier must be provided), false to pause.",
    )
    schedule_tier: str | None = Field(
        default=None,
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.",
    )

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_fields(self) -> "PatchIngestionConfigRequest":
        if self.mode == "passive":
            if self.schedule_tier is not None:
                raise ValueError("schedule_tier is not allowed for passive mode")
            if self.locator is not None:
                raise ValueError("locator is not allowed for passive mode")
            if self.auth is not None:
                raise ValueError("auth is not allowed for passive mode")
        if self.mode == "active-custom" and self.is_enabled is True and self.schedule_tier is None:
            raise ValueError(
                "schedule_tier must be provided in the same patch when setting "
                "is_enabled to true and mode to active-custom"
            )
        if self.mode is None and self.is_enabled is True and self.schedule_tier is None:
            raise ValueError(
                "schedule_tier must be provided in the same patch when setting is_enabled to true"
            )
        if self.platform is not None:
            from src.shared.models.ingestion import PLATFORM_REGISTRY

            locator_cls, identifier_cls, auth_cls = PLATFORM_REGISTRY[self.platform]
            if self.locator is not None:
                locator_cls.model_validate(self.locator)
            if self.identifier is not None:
                identifier_cls.model_validate(self.identifier)
            if self.auth is not None:
                # Pass only the persisted shape so CredentialAuth (extra="forbid")
                # does not reject transient API fields (password, force_overwrite).
                auth_payload: dict[str, Any] = {"username": self.auth.username}
                if self.auth.secret_ref is not None:
                    auth_payload["secret_ref"] = {
                        "name": self.auth.secret_ref.name,
                        "key": self.auth.secret_ref.key,
                    }
                auth_cls.model_validate(auth_payload)
        return self


class RunIngestionRequest(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="When true, validate and simulate the ingestion without writing any data",
    )


class IngestionConfigResponse(SingleResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Unique identifier of the ingestion config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    mode: str = Field(
        default="active-custom",
        description=(
            "Ingestion mode: 'active-custom' (DataSpoke's in-house extractor) or "
            "'passive' (externally-run; DataSpoke mirrors run history)"
        ),
    )
    platform: Platform = Field(description="Data platform, e.g. 'postgres'")
    locator: dict[str, Any] | None = Field(
        default=None,
        description="Infrastructure location configuration (`active-custom` only; null for `passive`)",
    )
    identifier: dict[str, Any] = Field(
        description="Dataset identity within the source infrastructure"
    )
    auth: dict[str, Any] | None = Field(
        description="Access credentials (reference shape only — no plaintext passwords)"
    )
    is_enabled: bool = Field(description="Whether scheduled ingestion runs are enabled")
    schedule_tier: str | None = Field(
        description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'"
    )
    workflow_dag_id: str | None = Field(
        description="Airflow DAG ID for the registered ingestion DAG"
    )
    status: IngestionConfigStatus = Field(
        description="DAG verification outcome: 'OK' (DAG registered and ready) or 'ERROR' (verification failed)"
    )
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> str:
        return v if isinstance(v, str) else str(v)


class IngestionConfigListResponse(PaginatedResponse):
    configs: list[IngestionConfigResponse] = Field(
        default=[], description="Page of ingestion config records"
    )


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Airflow DAG run ID for this run")
    status: str = Field(
        description="Execution status returned by Airflow, e.g. 'running' or 'success'"
    )
    detail: dict[str, Any] = Field(
        default={}, description="Additional execution metadata returned by Airflow"
    )
