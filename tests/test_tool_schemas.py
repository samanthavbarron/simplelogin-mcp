"""Guards on the published tool schemas.

These exist because of a production failure on 2026-08-02: LiteLLM's MCP gateway
validates arguments against the schema we publish *before* forwarding them, and
passes values through as strings. An optional int renders as
``anyOf: [{"type": "integer"}, {"type": "null"}]``, which a string satisfies
neither branch of, so every paginated call was rejected with "'0' is not valid
under any of the given schemas".

Nothing server-side can fix that -- the rejection happens upstream. So the
schema shape itself is the contract, and it is asserted here.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastmcp import FastMCP

from simplelogin_mcp.permissions import PermissionLevel

PAGINATED_TOOLS = [
    "list_aliases",
    "search_aliases",
    "get_alias_activities",
    "list_alias_contacts",
]


async def schema_for(server_for: Callable[..., FastMCP], tool_name: str) -> dict:
    server = server_for(PermissionLevel.DELETE)
    for tool in await server.list_tools():
        if tool.name == tool_name:
            return tool.parameters
    raise AssertionError(f"tool {tool_name} not registered")


class TestPageIdSchema:
    @pytest.mark.parametrize("tool_name", PAGINATED_TOOLS)
    async def test_page_id_is_a_plain_integer_not_a_union(
        self, server_for: Callable[..., FastMCP], tool_name: str
    ) -> None:
        """The regression guard. anyOf here is what broke pagination in prod."""
        schema = await schema_for(server_for, tool_name)
        page_id = schema["properties"]["page_id"]

        assert "anyOf" not in page_id, (
            f"{tool_name}.page_id renders as a union, which LiteLLM's gateway "
            "rejects when it passes the value through as a string"
        )
        assert page_id["type"] == "integer"

    @pytest.mark.parametrize("tool_name", PAGINATED_TOOLS)
    async def test_page_id_is_optional(
        self, server_for: Callable[..., FastMCP], tool_name: str
    ) -> None:
        schema = await schema_for(server_for, tool_name)
        assert "page_id" not in schema.get("required", [])

    @pytest.mark.parametrize("tool_name", PAGINATED_TOOLS)
    async def test_sentinel_is_accepted_by_the_schema(
        self, server_for: Callable[..., FastMCP], tool_name: str
    ) -> None:
        """-1 must be within the declared bounds, or clients cannot send it."""
        schema = await schema_for(server_for, tool_name)
        assert schema["properties"]["page_id"]["minimum"] <= -1
