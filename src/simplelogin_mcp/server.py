"""Server assembly: permission gating, optional bearer auth, health endpoint."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .client import SimpleLoginClient
from .config import Settings
from .errors import PermissionDeniedError
from .permissions import PermissionLevel, required_level
from .tools import register_tools

SERVER_NAME = "simplelogin-mcp"
HEALTH_PATH = "/health"


class PermissionMiddleware(Middleware):
    """Enforces the deployment's permission level, at two independent layers.

    ``on_list_tools`` hides tools the deployment may not use, so a well-behaved
    client never sees them. ``on_call_tool`` refuses them regardless of what was
    listed, so a client that guesses or hard-codes a name gains nothing. Neither
    layer relies on the other being present.
    """

    def __init__(self, level: PermissionLevel) -> None:
        self.level = level

    def _permitted(self, tags: object) -> bool:
        required = required_level(tags)
        # Fail closed. An untagged tool is a registration bug, and treating it
        # as universally available would hand out capabilities silently.
        return required is not None and self.level.permits(required)

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Sequence[Any]]
    ) -> Sequence[Any]:
        tools = await call_next(context)
        return [tool for tool in tools if self._permitted(tool.tags)]

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        name = context.message.name
        server: FastMCP = context.fastmcp_context.fastmcp
        tool = await server.get_tool(name)

        if tool is None:
            # Genuinely unknown: let FastMCP raise its own not-found error rather
            # than inventing a permission error for a tool that does not exist.
            return await call_next(context)

        required = required_level(tool.tags)
        if required is None or not self.level.permits(required):
            raise ToolError(
                str(
                    PermissionDeniedError(
                        tool_name=name,
                        required=required.name.lower() if required else "unknown",
                        configured=self.level.name.lower(),
                    )
                )
            )
        return await call_next(context)


class BearerTokenMiddleware:
    """Requires a static bearer token on every request except the health probe.

    The health endpoint stays open so orchestrators can probe liveness without
    being issued a credential.
    """

    def __init__(self, app: ASGIApp, token: str, exempt_paths: frozenset[str]) -> None:
        self.app = app
        self.token = token
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        header = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value.decode("latin-1")
                break

        scheme, _, credential = header.partition(" ")
        # compare_digest keeps the comparison constant-time; the scheme check is
        # case-insensitive per RFC 7235.
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            credential.strip(), self.token
        ):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_server(
    settings: Settings, *, client: SimpleLoginClient | None = None
) -> FastMCP:
    """Construct the MCP server for a given configuration.

    ``client`` may be injected for testing; otherwise one is built from settings
    and closed on shutdown.
    """
    owns_client = client is None
    sl_client = client or SimpleLoginClient(
        settings.api_key.get_secret_value(),
        base_url=settings.api_base_url,
        timeout=settings.request_timeout,
    )

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_client:
                await sl_client.aclose()

    mcp: FastMCP = FastMCP(
        name=SERVER_NAME,
        version="0.1.0",
        instructions=(
            "Manage SimpleLogin email aliases. This deployment is configured at "
            f"permission level '{settings.permission_level.name.lower()}'; tools "
            "above that level are not available. Aliases cannot be deleted through "
            "this server -- use toggle_alias to disable one instead."
        ),
        middleware=[PermissionMiddleware(settings.permission_level)],
        lifespan=lifespan,
    )

    register_tools(mcp, sl_client, max_auto_pages=settings.max_auto_pages)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Any) -> JSONResponse:
        # Liveness only. Deliberately does not call SimpleLogin: an orchestrator
        # probing every few seconds would burn rate limit, and upstream being
        # down is not a reason to restart this container.
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "permission_level": settings.permission_level.name.lower(),
            }
        )

    return mcp


def build_http_app(settings: Settings, *, client: SimpleLoginClient | None = None) -> Any:
    """Build the Starlette app, wrapping it in bearer auth when configured."""
    mcp = build_server(settings, client=client)

    asgi_middleware: list[ASGIMiddleware] = []
    if settings.auth_token is not None:
        asgi_middleware.append(
            ASGIMiddleware(
                BearerTokenMiddleware,
                token=settings.auth_token.get_secret_value(),
                exempt_paths=frozenset({HEALTH_PATH}),
            )
        )

    return mcp.http_app(
        path=settings.path,
        transport="http",
        middleware=asgi_middleware or None,
    )
