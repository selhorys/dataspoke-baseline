"""Re-export Settings from the shared layer for backward compatibility.

The canonical definition lives in src/shared/settings.py so that all layers
(shared, workflows, backend) can import settings without depending on src/api.
"""

from src.shared.settings import Settings, settings

__all__ = ["Settings", "settings"]
