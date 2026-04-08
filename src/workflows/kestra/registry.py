"""Register Kestra flow definitions from YAML files.

Flow Registration Lifecycle
───────────────────────────
Kestra flows are registered at two points:

1. **Server startup (lifespan)** — ``register_all_flows()`` is called
   during the FastAPI lifespan.  All startup flows are registered in a
   simple loop.

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

import logging
from pathlib import Path

import yaml

from src.shared.settings import settings
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


async def register_all_flows(client: KestraClient) -> int:
    """Register Kestra flows from src/workflows/flows/.

    Returns the number of flows successfully registered.
    """
    if not FLOWS_DIR.is_dir():
        logger.warning("Flows directory not found: %s", FLOWS_DIR)
        return 0

    count = 0
    yaml_files = sorted(f for f in FLOWS_DIR.glob("*.yaml") if f.name in _STARTUP_FLOWS)
    for yaml_file in yaml_files:
        flow_yaml = yaml_file.read_text()
        if settings.kestra_callback_base_url != "http://dataspoke-api:8002":
            flow_yaml = flow_yaml.replace(
                "http://dataspoke-api:8002",
                settings.kestra_callback_base_url,
            )
        flow_id = yaml.safe_load(flow_yaml).get("id", yaml_file.stem)
        try:
            await client.create_or_update_flow(flow_yaml)
            logger.info("Registered flow %s", flow_id)
            count += 1
        except Exception:
            logger.error("Failed to register flow from %s", yaml_file.name, exc_info=True)

    return count
