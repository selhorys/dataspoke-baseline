"""Unit tests for notification configuration (config.py).

Tests spec-mandated defaults and env-prefix convention for NotificationSettings:
- notification_enabled defaults to False (no-op mode).
- Settings class uses DATASPOKE_ env prefix (all application env vars share this prefix).

spec: feature/BACKEND.md §Notifications — Master toggle DATASPOKE_NOTIFICATION_ENABLED
      (default false — no-ops in dev).
spec: feature/BACKEND.md §Configuration — Settings class reads DATASPOKE_* env vars.
"""

from src.shared.notifications.config import NotificationSettings


def test_notification_enabled_defaults_to_false() -> None:
    """notification_enabled must default to False (no-op mode by default).

    spec: feature/BACKEND.md §Notifications — notification_enabled=False disables email delivery.
    """
    cfg = NotificationSettings()
    assert cfg.notification_enabled is False, (
        "notification_enabled must default to False so no emails are sent unless explicitly "
        "enabled. spec: feature/BACKEND.md §Notifications."
    )


def test_notification_settings_env_prefix() -> None:
    """NotificationSettings uses DATASPOKE_ env prefix.

    spec: feature/BACKEND.md §Configuration (line 891) — Settings class reads DATASPOKE_* env vars.
    spec: ARCHITECTURE.md §Environment Variables — application runtime variables use DATASPOKE_* prefix.
    """
    # model_config is exposed as class attribute in Pydantic v2
    prefix = NotificationSettings.model_config.get("env_prefix", "")
    assert prefix == "DATASPOKE_", (
        f"Expected env_prefix='DATASPOKE_', got {prefix!r}. "
        "spec: feature/BACKEND.md §Configuration."
    )
