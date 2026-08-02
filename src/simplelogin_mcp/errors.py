"""Error types for the SimpleLogin client.

The client layer stays free of MCP concepts so it can be tested on its own; the
tool layer converts these into ``ToolError``.
"""

from __future__ import annotations


class SimpleLoginError(Exception):
    """Base class for all SimpleLogin API failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthenticationError(SimpleLoginError):
    """The API key was rejected (HTTP 401)."""


class UpgradeRequiredError(SimpleLoginError):
    """The account's plan does not permit this operation (HTTP 403).

    Most commonly reverse-alias (contact) creation on a free account.
    """


class NotFoundError(SimpleLoginError):
    """The referenced entity does not exist, or is not owned by this account."""


class RateLimitedError(SimpleLoginError):
    """SimpleLogin is throttling us (HTTP 429)."""


class PermissionDeniedError(Exception):
    """Raised locally when a tool exceeds the deployment's permission level.

    This never reaches the SimpleLogin API -- it is our own gate, refused before
    any request is made.
    """

    def __init__(self, tool_name: str, required: str, configured: str) -> None:
        super().__init__(
            f"Tool {tool_name!r} requires permission level {required!r}, but this "
            f"deployment is configured at {configured!r}. The operation was refused "
            f"locally and no request was sent to SimpleLogin."
        )
        self.tool_name = tool_name
        self.required = required
        self.configured = configured
