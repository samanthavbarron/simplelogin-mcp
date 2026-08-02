"""HTTP surface: the health probe and the optional bearer token."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from simplelogin_mcp.client import SimpleLoginClient
from simplelogin_mcp.config import Settings
from simplelogin_mcp.server import build_http_app

from .fake_simplelogin import FakeSimpleLogin

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


BASE_URL = "http://simplelogin.test"


def build(**overrides: Any) -> tuple[Any, FakeSimpleLogin]:
    """Build an app wired to a fresh fake, returning both so tests can inspect it."""
    fake = FakeSimpleLogin()
    settings = Settings(
        **{
            "SIMPLELOGIN_API_KEY": "test-key",
            "SIMPLELOGIN_API_BASE_URL": BASE_URL,
            **overrides,
        }
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app()),
        base_url=BASE_URL,
        headers={"Authentication": fake.api_key},
    )
    client = SimpleLoginClient("test-key", base_url=BASE_URL, http_client=http)
    return build_http_app(settings, client=client), fake


def app_for(**overrides: Any) -> Any:
    return build(**overrides)[0]


class TestHealthEndpoint:
    def test_reports_ok_and_the_configured_level(self) -> None:
        with TestClient(app_for(SIMPLELOGIN_PERMISSION_LEVEL="update")) as client:
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["permission_level"] == "update"

    def test_stays_open_when_bearer_auth_is_enabled(self) -> None:
        """Orchestrators must be able to probe liveness without a credential."""
        with TestClient(app_for(MCP_AUTH_TOKEN="secret")) as client:
            response = client.get("/health")

        assert response.status_code == 200

    def test_does_not_call_simplelogin(self) -> None:
        """Probing every few seconds must not consume upstream rate limit."""
        app, fake = build()

        with TestClient(app) as client:
            for _ in range(5):
                client.get("/health")

        assert fake.request_log == []

    def test_never_leaks_credentials(self) -> None:
        with TestClient(
            app_for(MCP_AUTH_TOKEN="super-secret", SIMPLELOGIN_API_KEY="key-material")
        ) as client:
            body = client.get("/health").text

        assert "super-secret" not in body
        assert "key-material" not in body


class TestBearerAuthDisabled:
    def test_mcp_endpoint_is_open_when_no_token_configured(self) -> None:
        with TestClient(app_for()) as client:
            response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

        assert response.status_code != 401


class TestBearerAuthEnabled:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="no-header"),
            pytest.param({"Authorization": "Bearer wrong"}, id="wrong-token"),
            pytest.param({"Authorization": "secret"}, id="missing-scheme"),
            pytest.param({"Authorization": "Basic secret"}, id="wrong-scheme"),
            pytest.param({"Authorization": "Bearer "}, id="empty-token"),
            pytest.param({"Authorization": "Bearer secret extra"}, id="trailing-junk"),
        ],
    )
    def test_rejects_bad_credentials(self, headers: dict[str, str]) -> None:
        with TestClient(app_for(MCP_AUTH_TOKEN="secret")) as client:
            response = client.post(
                "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers}
            )

        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_accepts_the_configured_token(self) -> None:
        with TestClient(app_for(MCP_AUTH_TOKEN="secret")) as client:
            response = client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": "Bearer secret"},
            )

        assert response.status_code != 401

    def test_scheme_matching_is_case_insensitive(self) -> None:
        """RFC 7235 makes the scheme case-insensitive; some clients send 'bearer'."""
        with TestClient(app_for(MCP_AUTH_TOKEN="secret")) as client:
            response = client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": "bearer secret"},
            )

        assert response.status_code != 401

    def test_rejection_happens_before_any_upstream_call(self) -> None:
        app, fake = build(MCP_AUTH_TOKEN="secret")

        with TestClient(app) as client:
            client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

        assert fake.request_log == []
