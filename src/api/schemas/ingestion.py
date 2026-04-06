"""Ingestion config CRUD and run request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import IngestionConfigStatus
from src.shared.models.ingestion import (
    NoAuth,
    Platform,
    validate_platform_fields,
)


class CreateIngestionConfigRequest(BaseModel):
    dataset_urn: str = Field(description="DataHub URN of the dataset to ingest, e.g. 'urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)'")
    platform: Platform = Field(description="Data platform that determines the locator/identifier/auth structure")
    locator: dict[str, Any] = Field(
        description=(
            "Infrastructure location. Structure varies by platform:\n"
            "- postgres/mysql/oracle: {\"host\": \"db.example.com\", \"port\": 5432}\n"
            "- bigquery: {\"project_id\": \"my-project\"}\n"
            "- snowflake: {\"account_id\": \"abc12345\"}\n"
            "- kafka: {\"bootstrap_servers\": \"kafka:9092\"}"
        )
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
    auth: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Access credentials. Required for postgres/mysql/oracle/snowflake, omit for bigquery/kafka.\n"
            "Example: {\"username\": \"readonly\", \"secret_ref\": \"k8s-secret/db-password\"}"
        )
    )
    is_active: bool = Field(default=False, description="Whether the ingestion config is active and scheduled to run")
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic runs, e.g. '0 6 * * *' for daily at 06:00 UTC. Required when is_active is true.")
    enrichment_sources: dict[str, Any] | None = Field(default=None, description="Optional additional enrichment sources to merge after primary ingestion. Keys are enrichment source identifiers.")
    custom_extractors: dict[str, Any] | None = Field(default=None, description="Optional custom extractor configuration overrides. Keys are extractor names, values are extractor-specific settings.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)",
                "platform": "postgres",
                "locator": {"host": "db.example.com", "port": 5432},
                "identifier": {"database": "mydb", "schema_name": "public", "table": "orders"},
                "auth": {"username": "readonly", "secret_ref": "k8s-secret/db-password"},
                "is_active": True,
                "schedule_cron": "0 6 * * *",
            }
        }
    }

    @model_validator(mode="after")
    def validate_fields(self) -> "CreateIngestionConfigRequest":
        if self.is_active and not self.schedule_cron:
            raise ValueError("schedule_cron is required when is_active is true")
        validate_platform_fields(
            self.platform, self.locator, self.identifier, self.auth
        )
        return self


class PatchIngestionConfigRequest(BaseModel):
    platform: Platform | None = Field(default=None, description="Update the data platform (also re-validates locator/identifier/auth when provided)")
    locator: dict[str, Any] | None = Field(default=None, description="Updated infrastructure location dict. See CreateIngestionConfigRequest.locator for structure by platform.")
    identifier: dict[str, Any] | None = Field(default=None, description="Updated dataset identity dict. See CreateIngestionConfigRequest.identifier for structure by platform.")
    auth: dict[str, Any] | None = Field(default=None, description="Updated access credentials dict. See CreateIngestionConfigRequest.auth for structure.")
    is_active: bool | None = Field(default=None, description="Set to true to activate scheduling (schedule_cron must be provided in the same request), false to pause.")
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic runs, e.g. '0 6 * * *' for daily at 06:00 UTC.")
    enrichment_sources: dict[str, Any] | None = Field(default=None, description="Updated enrichment sources configuration.")
    custom_extractors: dict[str, Any] | None = Field(default=None, description="Updated custom extractor configuration.")

    @model_validator(mode="after")
    def validate_fields(self) -> "PatchIngestionConfigRequest":
        # Only validate when is_active is explicitly set to true in this patch.
        if self.is_active is True and self.schedule_cron is None:
            raise ValueError(
                "schedule_cron must be provided in the same patch when setting is_active to true"
            )
        # Per-platform sub-field validation only when platform is present.
        if self.platform is not None:
            from src.shared.models.ingestion import PLATFORM_REGISTRY

            locator_cls, identifier_cls, auth_cls = PLATFORM_REGISTRY[
                self.platform
            ]
            if self.locator is not None:
                locator_cls.model_validate(self.locator)
            if self.identifier is not None:
                identifier_cls.model_validate(self.identifier)
            if self.auth is not None:
                auth_cls.model_validate(self.auth)
        return self


class RunIngestionRequest(BaseModel):
    dry_run: bool = Field(default=False, description="When true, validate and simulate the ingestion without writing any data")


class IngestionConfigResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the ingestion config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    platform: Platform = Field(description="Data platform, e.g. 'postgres'")
    locator: dict[str, Any] = Field(description="Infrastructure location configuration")
    identifier: dict[str, Any] = Field(description="Dataset identity within the source infrastructure")
    auth: dict[str, Any] | None = Field(description="Access credentials (secret references only, no plaintext passwords)")
    is_active: bool = Field(description="Whether scheduled ingestion runs are enabled")
    schedule_cron: str | None = Field(description="Cron expression for scheduled runs")
    enrichment_sources: dict[str, Any] | None = Field(description="Enrichment sources configuration")
    custom_extractors: dict[str, Any] | None = Field(description="Custom extractor configuration")
    kestra_flow_namespace: str | None = Field(description="Kestra namespace of the registered ingestion flow")
    kestra_flow_id: str | None = Field(description="Kestra flow ID of the registered ingestion flow")
    status: IngestionConfigStatus = Field(description="Config lifecycle status: 'OK' (flow registered and ready) or 'draft' (not yet registered)")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class IngestionConfigListResponse(PaginatedResponse):
    configs: list[IngestionConfigResponse] = Field(default=[], description="Page of ingestion config records")


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Kestra execution ID for this run")
    status: str = Field(description="Execution status returned by Kestra, e.g. 'RUNNING' or 'SUCCESS'")
    detail: dict[str, Any] = Field(default={}, description="Additional execution metadata returned by Kestra")
