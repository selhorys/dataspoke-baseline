from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATASPOKE_", case_sensitive=False)

    # JWT
    jwt_secret_key: str = "changeme-dev-secret-do-not-use-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # Stub admin user for auth (dev/stub only; replaced by a real identity store later)
    admin_username: str = "admin"
    admin_password: str = "admin"
    admin_groups: list[str] = ["admin", "de", "da", "dg"]

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Rate limiting
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 20

    # DataHub connection
    datahub_gms_url: str = "http://localhost:8080"
    datahub_token: str = ""
    datahub_kafka_brokers: str = "localhost:9092"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "dataspoke"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_http_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_api_key: str = ""

    # Temporal
    temporal_host: str = "localhost"
    temporal_port: int = 7233
    temporal_namespace: str = "default"

    # LLM
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"

    # Notifications
    notification_enabled: bool = False
    notification_from: str = "dataspoke@example.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""


settings = Settings()
