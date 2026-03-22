"""Unit tests for DataHub SDK-based ingestion extractors."""

import pytest

from src.backend.ingestion.extractors import (
    SUPPORTED_SOURCE_TYPES,
    IngestionResult,
    build_ingestion_recipe,
    run_datahub_ingestion,
)

# ── SUPPORTED_SOURCE_TYPES ────────────────────────────────────────────────────


def test_supported_source_types_contains_expected():
    assert {"postgres", "mysql", "oracle", "bigquery", "snowflake", "kafka"}.issubset(
        SUPPORTED_SOURCE_TYPES
    )


# ── build_ingestion_recipe ────────────────────────────────────────────────────


def test_build_recipe_postgres():
    location = {"host": "db.example.com", "port": 5432, "database": "mydb", "username": "user", "secret_ref": "s3cr3t"}
    recipe = build_ingestion_recipe("postgres", location, "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)")
    assert recipe["source"]["type"] == "postgres"
    assert recipe["source"]["config"]["host_port"] == "db.example.com:5432"
    assert recipe["source"]["config"]["database"] == "mydb"
    assert recipe["source"]["config"]["username"] == "user"
    assert recipe["sink"]["type"] == "datahub-rest"


def test_build_recipe_mysql():
    location = {"host": "mysql.example.com", "port": 3306, "database": "shop", "username": "root", "secret_ref": "pw"}
    recipe = build_ingestion_recipe("mysql", location, "urn:li:dataset:(urn:li:dataPlatform:mysql,shop.orders,PROD)")
    assert recipe["source"]["type"] == "mysql"
    assert recipe["source"]["config"]["host_port"] == "mysql.example.com:3306"


def test_build_recipe_bigquery():
    location = {"project_id": "my-gcp-project"}
    recipe = build_ingestion_recipe("bigquery", location, "urn:li:dataset:(urn:li:dataPlatform:bigquery,my-gcp-project.ds.tbl,PROD)")
    assert recipe["source"]["type"] == "bigquery"
    assert recipe["source"]["config"]["project_id"] == "my-gcp-project"


def test_build_recipe_snowflake():
    location = {"account_id": "xy12345.us-east-1", "username": "svc", "secret_ref": "snowpw"}
    recipe = build_ingestion_recipe("snowflake", location, "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.tbl,PROD)")
    assert recipe["source"]["type"] == "snowflake"
    assert recipe["source"]["config"]["account_id"] == "xy12345.us-east-1"


def test_build_recipe_kafka():
    location = {"bootstrap_servers": "kafka:9092"}
    recipe = build_ingestion_recipe("kafka", location, "urn:li:dataset:(urn:li:dataPlatform:kafka,my-topic,PROD)")
    assert recipe["source"]["type"] == "kafka"
    assert recipe["source"]["config"]["connection"]["bootstrap"] == "kafka:9092"


def test_build_recipe_unsupported_type_raises():
    with pytest.raises(ValueError, match="Unsupported source_type"):
        build_ingestion_recipe("unsupported_db", {}, "urn:li:dataset:x")


# ── run_datahub_ingestion ─────────────────────────────────────────────────────


async def test_run_datahub_ingestion_returns_result_for_known_source():
    location = {"host": "db.example.com", "port": 5432, "database": "mydb", "username": "user", "secret_ref": "pw"}
    result = await run_datahub_ingestion(
        source_type="postgres",
        location=location,
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)",
        dry_run=False,
    )
    assert isinstance(result, IngestionResult)
    assert result.errors == []
    assert result.entities_ingested == 1


async def test_run_datahub_ingestion_dry_run_yields_zero_entities():
    location = {"host": "db.example.com", "port": 5432, "database": "mydb", "username": "user", "secret_ref": "pw"}
    result = await run_datahub_ingestion(
        source_type="postgres",
        location=location,
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)",
        dry_run=True,
    )
    assert result.entities_ingested == 0
    assert result.errors == []


async def test_run_datahub_ingestion_unsupported_source_returns_error():
    result = await run_datahub_ingestion(
        source_type="unknown_source",
        location={},
        dataset_urn="urn:li:dataset:x",
        dry_run=False,
    )
    assert result.entities_ingested == 0
    assert len(result.errors) == 1
    assert "Unsupported source_type" in result.errors[0]
