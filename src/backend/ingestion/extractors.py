"""DataHub SDK-based metadata ingestion for supported source types."""

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

SUPPORTED_SOURCE_TYPES = frozenset({"postgres", "mysql", "oracle", "bigquery", "snowflake", "kafka"})


class IngestionResult(BaseModel):
    """Result of a DataHub ingestion run."""

    entities_ingested: int
    errors: list[str]
    warnings: list[str]


def build_ingestion_recipe(source_type: str, location: dict[str, Any], dataset_urn: str) -> dict[str, Any]:
    """Build a DataHub ingestion recipe dict for the given source type and location."""
    if source_type not in SUPPORTED_SOURCE_TYPES:
        raise ValueError(f"Unsupported source_type: {source_type}")

    # Map source_type to DataHub source connector config
    source_config: dict[str, Any] = {"type": source_type, "config": {}}

    if source_type in ("postgres", "mysql", "oracle"):
        source_config["config"] = {
            "host_port": f"{location['host']}:{location['port']}",
            "database": location.get("database", ""),
            "username": location.get("username", ""),
            "password": location.get("secret_ref", ""),  # In prod, resolve secret_ref
        }
    elif source_type == "bigquery":
        source_config["config"] = {
            "project_id": location.get("project_id", ""),
        }
    elif source_type == "snowflake":
        source_config["config"] = {
            "account_id": location.get("account_id", ""),
            "username": location.get("username", ""),
            "password": location.get("secret_ref", ""),
        }
    elif source_type == "kafka":
        source_config["config"] = {
            "connection": {"bootstrap": location.get("bootstrap_servers", "")},
        }

    return {
        "source": source_config,
        "sink": {"type": "datahub-rest"},
    }


async def run_datahub_ingestion(
    source_type: str,
    location: dict[str, Any],
    dataset_urn: str,
    dry_run: bool = False,
) -> IngestionResult:
    """Run DataHub ingestion for the given source type and location.

    In the current implementation, this builds the recipe and simulates
    ingestion. Full DataHub Pipeline integration will be added when
    the acryl-datahub ingestion framework is wired up.
    """
    if source_type not in SUPPORTED_SOURCE_TYPES:
        return IngestionResult(
            entities_ingested=0,
            errors=[f"Unsupported source_type: {source_type}"],
            warnings=[],
        )

    recipe = build_ingestion_recipe(source_type, location, dataset_urn)
    logger.info(
        "run_datahub_ingestion",
        extra={
            "source_type": source_type,
            "dataset_urn": dataset_urn,
            "dry_run": dry_run,
        },
    )

    # TODO: Wire up actual DataHub Pipeline when acryl-datahub ingestion
    # framework is compatible with Python 3.13 runtime.
    # For now, return a placeholder result.
    return IngestionResult(
        entities_ingested=0 if dry_run else 1,
        errors=[],
        warnings=["DataHub Pipeline integration pending — placeholder result"],
    )
