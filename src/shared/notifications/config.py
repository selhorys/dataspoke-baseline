"""Notification settings for DataSpoke email delivery."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATASPOKE_", case_sensitive=False)

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notification_from: str = "dataspoke@example.com"
    notification_enabled: bool = False


notification_settings = NotificationSettings()
