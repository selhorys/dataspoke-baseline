"""Environment-driven configuration for DataSpoke.

All env-driven settings live here so that any layer (shared, workflows,
backend, api) can import them without violating the layered architecture
rule.

Runtime behavioral tunables (LLM provider/model, debate parameters, RAG k
values, confidence thresholds, validation window) are stored in the
``runtime_config`` DB table managed by ``src/backend/admin/config_service.py``.
Secrets and infra/auth/observability settings remain here.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATASPOKE_", case_sensitive=False)

    # JWT
    jwt_secret_key: str = "changeme-dev-secret-do-not-use-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # Google OAuth
    oauth_state_secret: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # Cookie flags
    cookie_secure: bool = False

    # Post-OAuth-login redirect target.  The SPA calls POST /auth/token/refresh
    # from this page to obtain an access token using the HttpOnly refresh cookie.
    oauth_post_login_redirect: str = "/"

    # Internal auth — shared secret for Airflow-to-API calls (blank disables internal endpoints)
    internal_token: str = ""

    # CORS (comma-separated string from env)
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]

    # Rate limiting
    rate_limit_per_minute: int = 120

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    # Airflow
    airflow_url: str = "http://localhost:8080"
    airflow_user: str = ""
    airflow_password: str = ""
    airflow_callback_base_url: str = "http://dataspoke-api:8002"

settings = Settings()
