"""Register Kestra flow definitions from YAML files.

Flow Registration Lifecycle
───────────────────────────
Kestra flows are registered at two points:

1. **Server startup (lifespan)** — ``register_all_flows()`` is called
   during the FastAPI lifespan.  Only flows listed in ``_STARTUP_FLOWS``
   are registered at this stage.  Currently this is limited to
   ``ingestion_config_sync.yaml`` to avoid overloading Kestra.

2. **Dynamic registration** — Per-dataset periodic ingestion flows are
   synced separately by the ``ingestion-config-sync`` Kestra flow
   (cron-triggered) or by ``sync_periodic_ingestion_flows()`` at startup.
   These are dynamically generated from ingestion configs in the DB.

The ``require_server`` fixture in ``tests/integration/api_wired/conftest.py``
verifies that startup flows are registered before any API-wired test runs.
If the server started but Kestra was unreachable during lifespan, flows may
be missing and the fixture will fail with a clear diagnostic.

To register additional flows at startup, add their YAML filename to
``_STARTUP_FLOWS``.  Flow YAML files live in ``src/workflows/flows/``.
"""

import logging
from pathlib import Path

from src.workflows.kestra.client import KestraClient

logger = logging.getLogger(__name__)

FLOWS_DIR = Path(__file__).resolve().parents[1] / "flows"

# Only register ingestion-related flows at startup.
# TODO: register other flows once Kestra can handle the load
#   (validation, generation, metrics, embedding_sync, ontology_rebuild, sla_monitor)
_STARTUP_FLOWS = frozenset({"ingestion_config_sync.yaml"})


async def register_all_flows(client: KestraClient) -> int:
    """Register ingestion-related Kestra flows from src/workflows/flows/.

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
        if yaml_file.name not in _STARTUP_FLOWS:
            continue
        flow_yaml = yaml_file.read_text()
        try:
            await client.create_or_update_flow(flow_yaml)
            logger.info("Registered flow from %s", yaml_file.name)
            count += 1
        except Exception:
            logger.error("Failed to register flow from %s", yaml_file.name, exc_info=True)

    return count
