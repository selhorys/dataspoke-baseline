"""Static resilience and tuning constants for DataSpoke backend.

These are compile-time tuning values, not env-driven settings.
Environment-driven configuration lives in src/api/config.py (Settings class).
"""

# DataHub client retry / circuit breaker
RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_MS: int = 500
CIRCUIT_BREAKER_THRESHOLD: int = 5
CIRCUIT_BREAKER_RESET_MS: int = 60000

# Bulk DataHub scan batching
BULK_BATCH_SIZE: int = 100
BULK_BATCH_DELAY_MS: int = 100

# Redis cache TTLs (seconds)
QUALITY_SCORE_CACHE_TTL: int = 300
VALIDATION_RESULT_CACHE_TTL: int = 60
SEARCH_RESULT_CACHE_TTL: int = 120

# Vector search
EMBEDDING_DIMENSION: int = 1536
SEARCH_SCORE_THRESHOLD: float = 0.3
EMBEDDING_COLLECTION: str = "dataset_embeddings"
EMBEDDING_MODEL_OPENAI: str = "text-embedding-3-small"
EMBEDDING_MODEL_GOOGLE: str = "models/text-embedding-004"

# Ontology
ONTOLOGY_CONFIDENCE_THRESHOLD: float = 0.7

# SLA monitoring
SLA_MONITOR_INTERVAL_MINUTES: int = 30
SLA_ALERT_BEFORE_MINUTES: int = 120
