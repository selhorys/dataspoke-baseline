"""Shared fixtures for all DataSpoke unit tests.

Provides common infrastructure mock fixtures used across api/, backend/,
shared/, and workflows/ test suites.
"""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def datahub():
    """Mock DataHub client — no real GMS connection."""
    return AsyncMock()


@pytest.fixture
def db():
    """Mock async DB session — no real PostgreSQL connection."""
    return AsyncMock()


@pytest.fixture
def cache():
    """Mock Redis client — no real Redis connection."""
    return AsyncMock()


@pytest.fixture
def llm():
    """Mock LLM client — no real LLM API calls."""
    return AsyncMock()


@pytest.fixture
def qdrant():
    """Mock Qdrant client — no real vector DB connection."""
    return AsyncMock()


@pytest.fixture
def notification():
    """Mock notification service — no real email sends."""
    return AsyncMock()
