"""API-wired integration test fixtures.

Extends the root ``tests/integration/conftest.py`` (inherited automatically
by pytest).  Provides fixtures specific to REST-based testing so that spot
and story tests get a ready-to-use auth header dict without boilerplate.
"""

import pytest

from tests.integration.conftest import _auth_headers


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """JWT auth headers for API-wired test requests."""
    return _auth_headers()
