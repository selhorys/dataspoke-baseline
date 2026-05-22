"""Shared fixtures for metrics measurer unit tests."""

from unittest.mock import AsyncMock

import pytest

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO


@pytest.fixture(autouse=True)
def patch_get_runtime_config(monkeypatch):
    """Auto-patch get_runtime_config for all metrics measurer unit tests.

    Measurer functions call get_runtime_config(db) to read the validation
    window interval setting. Unit tests use a mock DB session, so the runtime
    config must be stubbed. The stub returns factory-default values.
    """
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    monkeypatch.setattr(
        "src.backend.admin.config_service.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
    monkeypatch.setattr(
        "src.backend.metrics.measurers.validation_score.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
