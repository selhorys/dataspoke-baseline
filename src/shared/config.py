"""Static resilience and tuning constants for DataSpoke backend.

These are compile-time tuning values, not env-driven settings.
Environment-driven configuration lives in src/api/config.py (Settings class).
"""

# DataHub client retry / circuit breaker
RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_MS: int = 500
CIRCUIT_BREAKER_THRESHOLD: int = 5
CIRCUIT_BREAKER_RESET_MS: int = 60000

# Vector search
EMBEDDING_DIMENSION: int = 1536
EMBEDDING_COLLECTION: str = "dataset_embeddings"
EMBEDDING_MODEL_OPENAI: str = "text-embedding-3-small"
EMBEDDING_MODEL_GOOGLE: str = "models/gemini-embedding-001"

# Ontology
ONTOLOGY_CONFIDENCE_THRESHOLD: float = 0.7

# Kafka consumer tuning
CONSUMER_POLL_TIMEOUT_S: float = 1.0
HANDLER_TIMEOUT_S: int = 30
