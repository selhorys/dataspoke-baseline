"""Shared fixtures for metagen unit tests."""

from unittest.mock import AsyncMock

import pytest

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO


@pytest.fixture(autouse=True)
def patch_get_runtime_config(monkeypatch):
    """Auto-patch get_runtime_config for all metagen unit tests.

    Metagen service methods call get_runtime_config(db) at runtime. Unit tests
    use a mock DB session that isn't backed by a real database, so the runtime
    config must be stubbed to avoid DB round-trip failures.

    The default stub returns factory-default values (identical to the
    pre-migration settings defaults) so existing test assertions remain valid.
    Individual tests can override by patching again inside the test body.
    """
    fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
    monkeypatch.setattr(
        "src.backend.admin.config_service.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
    # Also patch the reference imported directly into metagen.service
    monkeypatch.setattr(
        "src.backend.metagen.service.get_runtime_config",
        AsyncMock(return_value=fake_rc),
    )
