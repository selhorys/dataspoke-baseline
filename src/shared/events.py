# Event-type constants for the unified DataSpoke event log.
#
# Convention: UPPERCASE dot-delimited "{DOMAIN}.{ACTION}".
# Full catalogue per spec/feature/BACKEND.md §Event Catalogue.
# Entity-type mapping per spec/feature/BACKEND_SCHEMA.md §events.

# ── Ingestion (entity_type varies: "ingestion_source" for source-level events,
#    "dataset" for dataset-level run events) ──────────────────────────────────

# Source-level management events (entity_type = "ingestion_source")
INGESTION_SOURCE_CREATE = "INGESTION.SOURCE_CREATE"
INGESTION_SOURCE_UPDATE = "INGESTION.SOURCE_UPDATE"
INGESTION_SOURCE_DELETE = "INGESTION.SOURCE_DELETE"

# Run events (entity_type = "ingestion_source", entity_id = source_id)
INGESTION_COMPLETE = "INGESTION.COMPLETE"
INGESTION_FAIL = "INGESTION.FAIL"
INGESTION_PREFIX = "INGESTION."

# ── Validation (entity_type = "dataset") ─────────────────────────────────────

VALIDATION_CONFIG_CREATE = "VALIDATION.CONFIG_CREATE"
VALIDATION_CONFIG_UPDATE = "VALIDATION.CONFIG_UPDATE"
VALIDATION_CONFIG_DELETE = "VALIDATION.CONFIG_DELETE"
VALIDATION_RESULT_RECORDED = "VALIDATION.RESULT_RECORDED"
VALIDATION_PREFIX = "VALIDATION."

# ── Metadata Generation (entity_type = "metagen" for run events,
#    entity_type = "dataset" for candidate review events) ──────────────────────

METAGEN_CONFIG_CREATE = "METAGEN.CONFIG_CREATE"
METAGEN_CONFIG_UPDATE = "METAGEN.CONFIG_UPDATE"
METAGEN_CONFIG_DELETE = "METAGEN.CONFIG_DELETE"
METAGEN_RUN_COMPLETE = "METAGEN.RUN_COMPLETE"
METAGEN_RUN_FAILED = "METAGEN.RUN_FAILED"
METAGEN_CANDIDATE_APPROVE = "METAGEN.CANDIDATE_APPROVE"
METAGEN_CANDIDATE_REJECT = "METAGEN.CANDIDATE_REJECT"
METAGEN_PREFIX = "METAGEN."

# ── Metrics (entity_type = "metric") ─────────────────────────────────────────

METRIC_CONFIG_CREATE = "METRIC.CONFIG_CREATE"
METRIC_CONFIG_UPDATE = "METRIC.CONFIG_UPDATE"
METRIC_CONFIG_DELETE = "METRIC.CONFIG_DELETE"
METRIC_RUN_COMPLETE = "METRIC.RUN_COMPLETE"
METRIC_PREFIX = "METRIC."

# ── Ontology Generation (entity_type = "ontogen") ────────────────────────────
# entity_id = "singleton" for conf events; "seed:{seed_id}" for seed events.

ONTOGEN_CONFIG_CREATE = "ONTOGEN.CONFIG_CREATE"
ONTOGEN_CONFIG_UPDATE = "ONTOGEN.CONFIG_UPDATE"
ONTOGEN_CONFIG_DELETE = "ONTOGEN.CONFIG_DELETE"
ONTOGEN_SEED_CREATE = "ONTOGEN.SEED_CREATE"
ONTOGEN_SEED_UPDATE = "ONTOGEN.SEED_UPDATE"
ONTOGEN_SEED_DELETE = "ONTOGEN.SEED_DELETE"
ONTOGEN_RUN_COMPLETE = "ONTOGEN.RUN_COMPLETE"
ONTOGEN_RUN_FAILED = "ONTOGEN.RUN_FAILED"
ONTOGEN_PREFIX = "ONTOGEN."

# ── Node review (entity_type = "node") ───────────────────────────────────────

NODE_APPROVE = "NODE.APPROVE"
NODE_REJECT = "NODE.REJECT"
NODE_PREFIX = "NODE."

# ── Edge review (entity_type = "edge") ───────────────────────────────────────

EDGE_APPROVE = "EDGE.APPROVE"
EDGE_REJECT = "EDGE.REJECT"
EDGE_PREFIX = "EDGE."

# ── Triple review (entity_type = "triple") ───────────────────────────────────

TRIPLE_APPROVE = "TRIPLE.APPROVE"
TRIPLE_REJECT = "TRIPLE.REJECT"
TRIPLE_PREFIX = "TRIPLE."
