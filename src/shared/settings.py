"""Environment-driven configuration for DataSpoke.

All env-driven settings live here so that any layer (shared, workflows,
backend, api) can import them without violating the layered architecture
rule.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATASPOKE_", case_sensitive=False)

    # JWT
    jwt_secret_key: str = "changeme-dev-secret-do-not-use-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # TBD(user-accounts): Remove stub admin settings when real identity store is in place
    admin_email: str = "admin"
    admin_password: str = "admin"
    admin_groups: list[str] = ["admin", "de", "da", "dg"]

    # Stub-auth gate — must be true for /auth/token to accept the stub admin credentials.
    # Production MUST keep this false until a real user store is wired.
    enable_stub_auth: bool = False

    # Internal auth — shared secret for Airflow-to-API calls (blank disables internal endpoints)
    internal_token: str = ""

    # Application ports
    api_port: int = 8002

    # CORS (comma-separated string from env)
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]

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

    # Airflow
    airflow_url: str = "http://localhost:8080"
    airflow_user: str = ""
    airflow_password: str = ""
    airflow_callback_base_url: str = "http://dataspoke-api:8002"
    airflow_ingestion_concurrent: int = 5

    # LLM
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"

    # Test mode (DATASPOKE_TEST_MODE) — when true, the ``make_*()`` factories
    # in ``src/workflows/_common.py`` return stub implementations instead of
    # real clients for LLM, pgvector, Redis (cache), and Notification.  DataHub
    # and PostgreSQL always use real connections regardless of this flag.
    # Enable via ``./dev_env/dataspoke-test-mode.sh`` or by exporting
    # ``DATASPOKE_TEST_MODE=true`` before starting the server.
    # See ``src/workflows/_stubs.py`` for stub behavior details.
    test_mode: bool = False

settings = Settings()
