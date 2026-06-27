"""Shared fixtures for all DataSpoke unit tests.

Provides common infrastructure mock fixtures used across api/, backend/,
shared/, and workflows/ test suites.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def datahub():
    """Mock DataHub client — no real GMS connection."""
    return AsyncMock()


@pytest.fixture
def db():
    """Mock async DB session — no real PostgreSQL connection.

    `spec=AsyncSession` keeps sync methods (add, delete, merge) as sync
    MagicMocks so `db.add(x)` doesn't return an un-awaited coroutine.
    """
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def cache():
    """Mock Redis client — no real Redis connection."""
    return AsyncMock()


@pytest.fixture
def llm():
    """Mock LLM client — no real LLM API calls."""
    return AsyncMock()


@pytest.fixture
def vector():
    """Mock PgVectorManager — no real pgvector DB connection."""
    return AsyncMock()
