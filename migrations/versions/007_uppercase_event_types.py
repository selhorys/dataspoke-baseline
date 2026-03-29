"""Rename event_type values to uppercase dot-separated format; fix ingestion CRUD status.

Revision ID: 007
Revises: 006
Create Date: 2026-03-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "007"
down_revision: str = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "dataspoke"

_RENAMES: list[tuple[str, str]] = [
    ("ingestion.config_created", "INGESTION.CONFIG_CREATE"),
    ("ingestion.config_updated", "INGESTION.CONFIG_UPDATE"),
    ("ingestion.config_deleted", "INGESTION.CONFIG_DELETE"),
    ("ingestion.completed", "INGESTION.COMPLETE"),
    ("ingestion.failed", "INGESTION.FAIL"),
    ("validation.completed", "VALIDATION.COMPLETE"),
    ("generation.completed", "GENERATION.COMPLETE"),
    ("generation.applied", "GENERATION.APPLY"),
    ("metric.run.completed", "METRIC.RUN_COMPLETE"),
    ("metric.alarm.triggered", "METRIC.ALARM_TRIGGER"),
    ("metric.findings.detected", "METRIC.FINDINGS_DETECT"),
    ("metric.activated", "METRIC.ACTIVATE"),
    ("metric.deactivated", "METRIC.DEACTIVATE"),
    ("concept.approved", "CONCEPT.APPROVE"),
    ("concept.rejected", "CONCEPT.REJECT"),
]

_INGESTION_CRUD_OLD = {"ingestion.config_created", "ingestion.config_updated", "ingestion.config_deleted"}


def upgrade() -> None:
    for old, new in _RENAMES:
        op.execute(
            f"UPDATE {SCHEMA}.events SET event_type = '{new}' WHERE event_type = '{old}'"
        )

    ingestion_crud_new = ("INGESTION.CONFIG_CREATE", "INGESTION.CONFIG_UPDATE", "INGESTION.CONFIG_DELETE")
    placeholders = ", ".join(f"'{v}'" for v in ingestion_crud_new)
    op.execute(
        f"UPDATE {SCHEMA}.events SET status = 'success' WHERE event_type IN ({placeholders}) AND status = 'ok'"
    )


def downgrade() -> None:
    ingestion_crud_new = ("INGESTION.CONFIG_CREATE", "INGESTION.CONFIG_UPDATE", "INGESTION.CONFIG_DELETE")
    placeholders = ", ".join(f"'{v}'" for v in ingestion_crud_new)
    op.execute(
        f"UPDATE {SCHEMA}.events SET status = 'ok' WHERE event_type IN ({placeholders}) AND status = 'success'"
    )

    for old, new in _RENAMES:
        op.execute(
            f"UPDATE {SCHEMA}.events SET event_type = '{old}' WHERE event_type = '{new}'"
        )
