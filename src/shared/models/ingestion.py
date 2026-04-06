"""Ingestion platform definitions and per-platform sub-models.

The Platform enum and sub-model registry live here (not in api/schemas)
so that both the API layer and the backend layer can import them without
circular dependencies.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


# ── Platform enum ─────────────────────────────────────────────────────────────


class Platform(str, Enum):
    POSTGRESQL = "postgres"
    MYSQL = "mysql"
    ORACLE = "oracle"
    BIGQUERY = "bigquery"
    SNOWFLAKE = "snowflake"
    KAFKA = "kafka"


# ── Locator sub-models (infra location) ──────────────────────────────────────


class RdbmsLocator(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    port: int


class KafkaLocator(BaseModel):
    model_config = ConfigDict(extra="allow")
    bootstrap_servers: str


class BigQueryLocator(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str


class SnowflakeLocator(BaseModel):
    model_config = ConfigDict(extra="allow")
    account_id: str


# ── Identifier sub-models (dataset identity within the infra) ────────────────


class RdbmsIdentifier(BaseModel):
    model_config = ConfigDict(extra="allow")
    database: str
    schema_name: str | None = None
    table: str | None = None


class KafkaIdentifier(BaseModel):
    model_config = ConfigDict(extra="allow")
    topic: str
    cluster: str | None = None


class BigQueryIdentifier(BaseModel):
    model_config = ConfigDict(extra="allow")
    dataset: str | None = None
    table: str | None = None


class SnowflakeIdentifier(BaseModel):
    model_config = ConfigDict(extra="allow")
    database: str | None = None
    schema_name: str | None = None
    table: str | None = None


# ── Auth sub-models (access credentials) ─────────────────────────────────────


class CredentialAuth(BaseModel):
    model_config = ConfigDict(extra="allow")
    username: str
    secret_ref: str | None = None
    password: str | None = None


class NoAuth(BaseModel):
    """Marker for platforms that use ambient / no explicit credentials."""

    model_config = ConfigDict(extra="allow")


# ── Registry ──────────────────────────────────────────────────────────────────

PLATFORM_REGISTRY: dict[
    Platform, tuple[type[BaseModel], type[BaseModel], type[BaseModel]]
] = {
    Platform.POSTGRESQL: (RdbmsLocator, RdbmsIdentifier, CredentialAuth),
    Platform.MYSQL: (RdbmsLocator, RdbmsIdentifier, CredentialAuth),
    Platform.ORACLE: (RdbmsLocator, RdbmsIdentifier, CredentialAuth),
    Platform.BIGQUERY: (BigQueryLocator, BigQueryIdentifier, NoAuth),
    Platform.SNOWFLAKE: (SnowflakeLocator, SnowflakeIdentifier, CredentialAuth),
    Platform.KAFKA: (KafkaLocator, KafkaIdentifier, NoAuth),
}


def validate_platform_fields(
    platform: Platform,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
) -> None:
    """Validate locator/identifier/auth dicts against the registry for *platform*.

    Raises ``ValueError`` on validation failure.
    """
    locator_cls, identifier_cls, auth_cls = PLATFORM_REGISTRY[platform]
    locator_cls.model_validate(locator)
    identifier_cls.model_validate(identifier)
    if auth is not None:
        auth_cls.model_validate(auth)
    elif auth_cls is not NoAuth:
        raise ValueError(f"auth is required for platform {platform.value}")
