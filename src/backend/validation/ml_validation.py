"""ML-based validation stub.

Full ML validation (range model, day-of-week baseline, etc.) is deferred
to the second pass.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def validate_values(
    dataset_urn: str,
    rule_id: str,
    values: dict[str, Any],
    ml_config: dict[str, Any],
    db: AsyncSession,
) -> dict[str, bool] | None:
    """Validate extracted values against historical ML models.

    Returns None — deferred to second pass.
    """
    return None
