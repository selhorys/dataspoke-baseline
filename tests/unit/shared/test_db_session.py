"""Unit tests for the async session factory — no DB connection needed."""

import inspect as stdlib_inspect
from unittest.mock import patch

from src.shared.db.session import SessionLocal, engine, get_session


def test_get_session_is_async_generator() -> None:
    assert stdlib_inspect.isasyncgenfunction(get_session)


def test_database_url_default() -> None:
    import importlib

    import src.shared.db.session as mod

    env_keys = [
        "DATASPOKE_POSTGRES_HOST",
        "DATASPOKE_POSTGRES_PORT",
        "DATASPOKE_POSTGRES_USER",
        "DATASPOKE_POSTGRES_PASSWORD",
        "DATASPOKE_POSTGRES_DB",
    ]
    clean_env = {k: v for k, v in __import__("os").environ.items() if k not in env_keys}
    with patch.dict("os.environ", clean_env, clear=True):
        importlib.reload(mod)
        assert (
            mod.DATABASE_URL == "postgresql+asyncpg://dataspoke:dataspoke@localhost:5432/dataspoke"
        )
    importlib.reload(mod)


def test_database_url_from_env() -> None:
    env = {
        "DATASPOKE_POSTGRES_HOST": "db.example.com",
        "DATASPOKE_POSTGRES_PORT": "9999",
        "DATASPOKE_POSTGRES_USER": "myuser",
        "DATASPOKE_POSTGRES_PASSWORD": "secret",
        "DATASPOKE_POSTGRES_DB": "mydb",
    }
    with patch.dict("os.environ", env, clear=False):
        import importlib

        import src.shared.db.session as mod

        importlib.reload(mod)
        assert mod.DATABASE_URL == "postgresql+asyncpg://myuser:secret@db.example.com:9999/mydb"

    # Reload to restore defaults
    importlib.reload(mod)


def test_engine_pool_size() -> None:
    assert engine.pool.size() == 10


def test_engine_max_overflow() -> None:
    assert engine.pool._max_overflow == 5


def test_session_factory_expire_on_commit_false() -> None:
    assert SessionLocal.kw.get("expire_on_commit") is False
