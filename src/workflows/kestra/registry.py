"""Register Kestra flow definitions from YAML files.

Flow Registration Lifecycle
───────────────────────────
Kestra flows are registered at two points:

1. **Server startup (lifespan)** — ``register_all_flows()`` is called
   during the FastAPI lifespan.  Flows are registered one-by-one with a
   verification GET and cooldown delay between each to avoid overwhelming
   Kestra in the dev cluster.

2. **Dynamic registration** — Per-dataset periodic ingestion flows are
   synced separately by the ``ingestion-config-sync`` Kestra flow
   (cron-triggered) or by ``sync_periodic_ingestion_flows()`` at startup.
   These are dynamically generated from ingestion configs in the DB.

The ``require_server`` fixture in ``tests/integration/api_wired/conftest.py``
verifies that all required flows are registered before any API-wired test
runs.  If the server started but Kestra was unreachable during lifespan,
flows may be missing and the fixture will fail with a clear diagnostic.

To register additional flows at startup, add their YAML filename to
``_STARTUP_FLOWS``.  Flow YAML files live in ``src/workflows/flows/``.
"""

import asyncio
import logging
from pathlib import Path

import yaml

from src.workflows.kestra.client import KestraClient

logger = logging.getLogger(__name__)

FLOWS_DIR = Path(__file__).resolve().parents[1] / "flows"

_STARTUP_FLOWS = frozenset({
    "ingestion_config_sync.yaml",
    "validation_config_sync.yaml",
    "metrics_config_sync.yaml",
    "generation.yaml",
    "metrics.yaml",
    "embedding_sync.yaml",
    "ontology_rebuild.yaml",
})

_REGISTER_COOLDOWN = 2.0  # seconds between registrations


async def register_all_flows(client: KestraClient) -> int:
    """Register Kestra flows one-by-one from src/workflows/flows/.

    Each flow is registered, then verified with a GET before proceeding
    to the next.  A cooldown delay is inserted between registrations to
    avoid overwhelming Kestra.

    Returns the number of flows successfully registered.
    """
    if not FLOWS_DIR.is_dir():
        logger.warning("Flows directory not found: %s", FLOWS_DIR)
        return 0

    count = 0
    yaml_files = sorted(f for f in FLOWS_DIR.glob("*.yaml") if f.name in _STARTUP_FLOWS)
    for i, yaml_file in enumerate(yaml_files):
        flow_yaml = yaml_file.read_text()
        flow_id = yaml.safe_load(flow_yaml).get("id", yaml_file.stem)
        try:
            await client.create_or_update_flow(flow_yaml)
            # Verify registration
            registered = await client.get_flow(flow_id)
            if registered is None:
                logger.error("Flow %s not found after registration", flow_id)
                continue
            logger.info("Registered flow %s (%d/%d)", flow_id, i + 1, len(yaml_files))
            count += 1
        except Exception:
            logger.error("Failed to register flow from %s", yaml_file.name, exc_info=True)

        # Cooldown between registrations (skip after the last one)
        if i < len(yaml_files) - 1:
            await asyncio.sleep(_REGISTER_COOLDOWN)

    return count
