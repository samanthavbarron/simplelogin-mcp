"""End-to-end tests against the built container image over real HTTP.

Everything here talks to a running container through the network, so it covers
what unit tests cannot: packaging, the entrypoint, the transport, and that the
permission level baked in via environment actually governs the live server.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_simplelogin import FakeSimpleLogin

from .conftest import container_env, image_config

pytestmark = pytest.mark.image

EXPECTED_TOOLS = {
    "read": {
        "get_alias_options",
        "list_aliases",
        "search_aliases",
        "get_alias",
        "get_alias_activities",
        "list_alias_contacts",
        "list_mailboxes",
    },
    "create": {"create_custom_alias", "create_random_alias", "create_alias_contact"},
    "update": {"update_alias", "toggle_alias"},
}


def tools_for(level: str) -> set[str]:
    cumulative: set[str] = set()
    for name in ("read", "create", "update"):
        cumulative |= EXPECTED_TOOLS[name]
        if name == level:
            return cumulative
    return cumulative  # delete adds nothing


def mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


class TestImageBasics:
    def test_health_endpoint_answers(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        _fake, upstream_port = fake_upstream
        _id, port = run_container(**container_env(upstream_port))

        body = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).json()

        assert body["status"] == "ok"
        assert body["permission_level"] == "read"

    def test_runs_as_a_non_root_user(self, engine: str, image: str) -> None:
        import subprocess

        result = subprocess.run(
            [engine, "run", "--rm", "--entrypoint", "", image, "id", "-u"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.stdout.strip() != "0", "container must not run as root"

    def test_declares_a_healthcheck(self, engine: str, image: str) -> None:
        """Orchestrators rely on this; podman's OCI format drops it, so skip there."""
        config = image_config(engine, image)
        healthcheck = config.get("Config", {}).get("Healthcheck") or config.get(
            "HealthCheck"
        )
        if healthcheck is None and "podman" in engine:
            pytest.skip("podman's OCI image format does not preserve HEALTHCHECK")
        assert healthcheck, "image declares no HEALTHCHECK"

    def test_refuses_to_start_without_an_api_key(
        self, engine: str, image: str
    ) -> None:
        """Misconfiguration must fail fast and say why, not boot broken."""
        import subprocess

        result = subprocess.run(
            [engine, "run", "--rm", image],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert "SIMPLELOGIN_API_KEY" in result.stdout + result.stderr

    def test_refuses_to_start_with_an_invalid_permission_level(
        self, engine: str, image: str
    ) -> None:
        import subprocess

        result = subprocess.run(
            [
                engine, "run", "--rm",
                "-e", "SIMPLELOGIN_API_KEY=dummy",
                "-e", "SIMPLELOGIN_PERMISSION_LEVEL=readonly",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "readonly" in combined
        assert "read, create, update, delete" in combined


class TestPermissionLevelsInTheImage:
    @pytest.mark.parametrize("level", ["read", "create", "update", "delete"])
    async def test_lists_only_permitted_tools(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
        level: str,
    ) -> None:
        _fake, upstream_port = fake_upstream
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_PERMISSION_LEVEL=level)
        )

        async with Client(mcp_url(port)) as client:
            listed = {tool.name for tool in await client.list_tools()}

        assert listed == tools_for(level)

    @pytest.mark.parametrize(
        ("level", "forbidden", "args"),
        [
            ("read", "create_random_alias", {}),
            ("read", "toggle_alias", {"alias_id": 47372146}),
            ("read", "update_alias", {"alias_id": 47372146, "note": "x"}),
            ("create", "toggle_alias", {"alias_id": 47372146}),
            ("create", "update_alias", {"alias_id": 47372146, "note": "x"}),
        ],
    )
    async def test_forbidden_tools_are_refused_over_the_wire(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
        level: str,
        forbidden: str,
        args: dict,
    ) -> None:
        """The deployed server must refuse even when the client names the tool."""
        fake, upstream_port = fake_upstream
        fake.add_alias(email="seed@aleeas.com")
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_PERMISSION_LEVEL=level)
        )

        async with Client(mcp_url(port)) as client:
            with pytest.raises(ToolError, match="requires permission level"):
                await client.call_tool(forbidden, args)

        assert fake.aliases[next(iter(fake.aliases))]["enabled"] is True

    async def test_a_read_deployment_cannot_mutate_anything(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        fake, upstream_port = fake_upstream
        alias = fake.add_alias(email="untouched@aleeas.com", note="original")
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_PERMISSION_LEVEL="read")
        )

        async with Client(mcp_url(port)) as client:
            assert (await client.call_tool("list_aliases", {})).data["aliases"]
            for tool, args in [
                ("create_random_alias", {}),
                ("update_alias", {"alias_id": alias["id"], "note": "changed"}),
                ("toggle_alias", {"alias_id": alias["id"]}),
            ]:
                with pytest.raises(ToolError):
                    await client.call_tool(tool, args)

        assert fake.aliases[alias["id"]]["note"] == "original"
        assert len(fake.aliases) == 1
        assert {method for method, _ in fake.request_log} <= {"GET", "POST"}

    async def test_delete_is_absent_at_every_level(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        """No level exposes a delete tool, and none issues a DELETE upstream."""
        fake, upstream_port = fake_upstream
        alias = fake.add_alias()
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_PERMISSION_LEVEL="delete")
        )

        async with Client(mcp_url(port)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert not [n for n in names if "delete" in n or "remove" in n]

            for attempt in ("delete_alias", "remove_alias", "alias_delete"):
                with pytest.raises(ToolError):
                    await client.call_tool(attempt, {"alias_id": alias["id"]})

        assert "DELETE" not in {method for method, _ in fake.request_log}
        assert fake.deleted_alias_ids == []
        assert alias["id"] in fake.aliases


class TestBearerAuthInTheImage:
    async def test_requires_the_token_when_configured(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        _fake, upstream_port = fake_upstream
        _id, port = run_container(
            **container_env(upstream_port, MCP_AUTH_TOKEN="s3cret")
        )

        unauthenticated = httpx.post(
            mcp_url(port),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=10,
        )
        assert unauthenticated.status_code == 401

        # Health stays reachable so orchestrators need no credential.
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200

        async with Client(
            mcp_url(port), auth="s3cret"
        ) as client:
            assert await client.list_tools()


class TestWorkflowThroughTheImage:
    async def test_full_alias_lifecycle_over_http(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        """The multi-step journey an agent would actually take, against the image."""
        fake, upstream_port = fake_upstream
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_PERMISSION_LEVEL="update")
        )

        async with Client(mcp_url(port)) as client:
            options = (
                await client.call_tool("get_alias_options", {"hostname": "shop.example"})
            ).data
            suffix = next(
                s["signed_suffix"] for s in options["suffixes"] if not s["is_premium"]
            )
            mailboxes = (await client.call_tool("list_mailboxes", {})).data

            created = (
                await client.call_tool(
                    "create_custom_alias",
                    {
                        "alias_prefix": "e2e",
                        "signed_suffix": suffix,
                        "mailbox_ids": [mailboxes["mailboxes"][0]["id"]],
                        "note": "created through the image",
                    },
                )
            ).data
            alias_id = created["id"]

            await client.call_tool(
                "update_alias", {"alias_id": alias_id, "note": "amended", "pinned": True}
            )
            fetched = (await client.call_tool("get_alias", {"alias_id": alias_id})).data
            assert fetched["note"] == "amended"
            assert fetched["pinned"] is True

            assert (
                await client.call_tool("toggle_alias", {"alias_id": alias_id})
            ).data["enabled"] is False

            contact = (
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": alias_id, "contact": "vendor@example.com"},
                )
            ).data
            assert contact["existed"] is False

            contacts = (
                await client.call_tool("list_alias_contacts", {"alias_id": alias_id})
            ).data
            assert len(contacts["contacts"]) == 1

        assert fake.deleted_alias_ids == []

    async def test_pagination_survives_the_transport(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        fake, upstream_port = fake_upstream
        for index in range(45):
            fake.add_alias(email=f"page{index}@aleeas.com")
        _id, port = run_container(**container_env(upstream_port))

        async with Client(mcp_url(port)) as client:
            everything = (await client.call_tool("list_aliases", {})).data
            first_page = (await client.call_tool("list_aliases", {"page_id": 0})).data

        assert len(everything["aliases"]) == 45
        assert everything["has_more"] is False
        assert len(first_page["aliases"]) == 20
        assert first_page["has_more"] is True

    async def test_upstream_errors_surface_intelligibly(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        _fake, upstream_port = fake_upstream
        _id, port = run_container(**container_env(upstream_port))

        async with Client(mcp_url(port)) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("get_alias", {"alias_id": 99999999})

        assert "not found" in str(excinfo.value).lower()

    async def test_a_wrong_api_key_is_reported_clearly(
        self,
        run_container: Callable[..., tuple[str, int]],
        fake_upstream: tuple[FakeSimpleLogin, int],
    ) -> None:
        _fake, upstream_port = fake_upstream
        _id, port = run_container(
            **container_env(upstream_port, SIMPLELOGIN_API_KEY="not-the-key")
        )

        async with Client(mcp_url(port)) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("list_aliases", {})

        assert "api key" in str(excinfo.value).lower()
