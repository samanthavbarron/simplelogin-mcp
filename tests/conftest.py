"""Shared fixtures wiring the server against the in-memory fake SimpleLogin."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastmcp import Client, FastMCP

from simplelogin_mcp.client import SimpleLoginClient
from simplelogin_mcp.config import Settings
from simplelogin_mcp.permissions import PermissionLevel
from simplelogin_mcp.server import build_server

from .fake_simplelogin import FakeSimpleLogin

FAKE_BASE_URL = "http://simplelogin.test"


@pytest.fixture
def fake() -> FakeSimpleLogin:
    return FakeSimpleLogin()


@pytest.fixture
async def sl_client(fake: FakeSimpleLogin) -> AsyncIterator[SimpleLoginClient]:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app()),
        base_url=FAKE_BASE_URL,
        headers={"Authentication": fake.api_key},
    )
    async with http:
        yield SimpleLoginClient(fake.api_key, base_url=FAKE_BASE_URL, http_client=http)


@pytest.fixture
def settings_for() -> Callable[..., Settings]:
    def _build(level: PermissionLevel | str = PermissionLevel.READ, **overrides: object):
        return Settings(
            SIMPLELOGIN_API_KEY="test-key",
            SIMPLELOGIN_API_BASE_URL=FAKE_BASE_URL,
            SIMPLELOGIN_PERMISSION_LEVEL=level,
            **overrides,
        )

    return _build


@pytest.fixture
def server_for(
    sl_client: SimpleLoginClient, settings_for: Callable[..., Settings]
) -> Callable[[PermissionLevel | str], FastMCP]:
    def _build(level: PermissionLevel | str) -> FastMCP:
        return build_server(settings_for(level), client=sl_client)

    return _build


@pytest.fixture
async def mcp_client_for_fake(
    settings_for: Callable[..., Settings],
) -> AsyncIterator[Callable[..., Client]]:
    """Build an MCP client against a caller-supplied fake.

    For tests that need a fake configured differently from the default (free
    plan, quota enforcement, custom page caps) rather than the shared one.
    """
    opened: list[httpx.AsyncClient] = []

    def _build(
        fake: FakeSimpleLogin,
        level: PermissionLevel | str = PermissionLevel.READ,
        **overrides: object,
    ) -> Client:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app()),
            base_url=FAKE_BASE_URL,
            headers={"Authentication": fake.api_key},
        )
        opened.append(http)
        sl_client = SimpleLoginClient(
            fake.api_key, base_url=FAKE_BASE_URL, http_client=http
        )
        return Client(build_server(settings_for(level, **overrides), client=sl_client))

    try:
        yield _build
    finally:
        for http in opened:
            await http.aclose()


@pytest.fixture
def mcp_client_for(
    server_for: Callable[[PermissionLevel | str], FastMCP],
) -> Callable[[PermissionLevel | str], Client]:
    """Return an in-memory MCP client factory, one per permission level.

    Going through a real client means requests traverse the middleware exactly
    as they would over HTTP, so the permission tests exercise the real path.
    """

    def _build(level: PermissionLevel | str) -> Client:
        return Client(server_for(level))

    return _build
