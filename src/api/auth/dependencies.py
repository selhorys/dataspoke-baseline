"""Auth dependency re-exports for the API layer.

All privilege logic lives in src.backend.auth.privilege.
This module re-exports the three standard dependencies so routers can import
from a single stable path.
"""

from src.backend.auth.privilege import (
    AuthContext,
    require_admin,
    require_authenticated,
    require_editor,
    require_writer,
)

__all__ = [
    "AuthContext",
    "require_authenticated",
    "require_writer",
    "require_editor",
    "require_admin",
]
