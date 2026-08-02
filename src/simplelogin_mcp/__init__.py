"""HTTP MCP server for a permission-gated subset of the SimpleLogin alias API."""

from .config import Settings
from .permissions import PermissionLevel
from .server import build_http_app, build_server

__version__ = "0.1.0"

__all__ = ["PermissionLevel", "Settings", "build_http_app", "build_server"]
