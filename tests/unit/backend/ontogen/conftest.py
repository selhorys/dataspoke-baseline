"""Shared fixtures for ontogen unit tests."""

from unittest.mock import AsyncMock

import pytest

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO


@pytest.fixture(autouse=True)
def patch_get_runtime_config(monkeypatch):
    """Auto-patch get_runtime_config for all ontogen unit tests.

    OntogenService._run_inner() calls get_runtime_config(db) to load debate
    parameters. Unit tests use mock DB sessions, so the runtime config must be
    stubbed to avoid DB round-trip failures. The stub returns factory defaults.
    """
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    monkeypatch.setattr(
        "src.backend.admin.config_service.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
    monkeypatch.setattr(
        "src.backend.ontogen.service.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
