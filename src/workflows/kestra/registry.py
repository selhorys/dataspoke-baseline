"""Register all Kestra flow definitions from YAML files."""

import logging
from pathlib import Path

from src.workflows.kestra.client import KestraClient

logger = logging.getLogger(__name__)

FLOWS_DIR = Path(__file__).resolve().parents[1] / "flows"


async def register_all_flows(client: KestraClient) -> int:
    """Read all YAML files from src/workflows/flows/ and register them with Kestra.

    Returns the number of flows registered.

    Note: Dynamic periodic ingestion flows are synced separately via the
    ``ingestion-config-sync`` Kestra flow (cron) or at app startup via
    ``sync_periodic_ingestion_flows()``.
    """
    if not FLOWS_DIR.is_dir():
        logger.warning("Flows directory not found: %s", FLOWS_DIR)
        return 0

    count = 0
    for yaml_file in sorted(FLOWS_DIR.glob("*.yaml")):
        flow_yaml = yaml_file.read_text()
        try:
            await client.create_or_update_flow(flow_yaml)
            logger.info("Registered flow from %s", yaml_file.name)
            count += 1
        except Exception:
            logger.error("Failed to register flow from %s", yaml_file.name, exc_info=True)

    return count
