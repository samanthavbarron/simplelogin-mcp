"""The permission model, exercised exhaustively.

Two properties matter and are tested separately, because each must hold on its
own:

1. Tools above the configured level are not advertised.
2. Tools above the configured level are refused when called anyway -- a client
   that hard-codes or guesses a name must gain nothing from doing so.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from simplelogin_mcp.permissions import PermissionLevel, required_level

from .fake_simplelogin import FakeSimpleLogin

SEEDED_ALIAS_ID = 47372146

#: The authoritative expectation. Kept as a literal rather than derived from the
#: tags so that an accidental retagging shows up as a test failure instead of
#: quietly redefining what the test is checking.
TOOL_LEVELS: dict[str, PermissionLevel] = {
    "get_alias_options": PermissionLevel.READ,
    "list_aliases": PermissionLevel.READ,
    "search_aliases": PermissionLevel.READ,
    "get_alias": PermissionLevel.READ,
    "get_alias_activities": PermissionLevel.READ,
    "list_alias_contacts": PermissionLevel.READ,
    "list_mailboxes": PermissionLevel.READ,
    "create_custom_alias": PermissionLevel.CREATE,
    "create_random_alias": PermissionLevel.CREATE,
    "create_alias_contact": PermissionLevel.CREATE,
    "update_alias": PermissionLevel.UPDATE,
    "toggle_alias": PermissionLevel.UPDATE,
}

#: Arguments valid enough to reach the API if permitted, so that a refusal is
#: unambiguously a permission refusal and not a validation error.
TOOL_ARGS: dict[str, dict] = {
    "get_alias_options": {},
    "list_aliases": {},
    "search_aliases": {"query": "seed"},
    "get_alias": {"alias_id": SEEDED_ALIAS_ID},
    "get_alias_activities": {"alias_id": SEEDED_ALIAS_ID},
    "list_alias_contacts": {"alias_id": SEEDED_ALIAS_ID},
    "list_mailboxes": {},
    "create_custom_alias": {
        "alias_prefix": "matrix",
        "signed_suffix": ".raffle542@aleeas.com.sig-free",
        "mailbox_ids": [14616811],
    },
    "create_random_alias": {},
    "create_alias_contact": {
        "alias_id": SEEDED_ALIAS_ID,
        "contact": "someone@example.com",
    },
    "update_alias": {"alias_id": SEEDED_ALIAS_ID, "note": "matrix"},
    "toggle_alias": {"alias_id": SEEDED_ALIAS_ID},
}

ALL_LEVELS = [
    PermissionLevel.READ,
    PermissionLevel.CREATE,
    PermissionLevel.UPDATE,
    PermissionLevel.DELETE,
]


@pytest.fixture(autouse=True)
def _seed(fake: FakeSimpleLogin) -> None:
    fake.add_alias(email="seed@aleeas.com", note="seed alias")


def expected_tools(level: PermissionLevel) -> set[str]:
    return {name for name, need in TOOL_LEVELS.items() if level.permits(need)}


class TestToolVisibility:
    @pytest.mark.parametrize("level", ALL_LEVELS, ids=lambda lv: lv.name.lower())
    async def test_lists_exactly_the_permitted_tools(
        self, mcp_client_for: Callable[..., Client], level: PermissionLevel
    ) -> None:
        async with mcp_client_for(level) as client:
            listed = {tool.name for tool in await client.list_tools()}
        assert listed == expected_tools(level)

    async def test_registry_and_expectations_agree(
        self, mcp_client_for: Callable[..., Client]
    ) -> None:
        """Guards against a tool being added without being classified here."""
        async with mcp_client_for(PermissionLevel.DELETE) as client:
            listed = {tool.name for tool in await client.list_tools()}
        assert listed == set(TOOL_LEVELS), (
            "tools registered on the server do not match TOOL_LEVELS; a new tool "
            "must be given an explicit expected permission level"
        )

    async def test_every_tool_carries_a_permission_tag(
        self, server_for: Callable[..., FastMCP]
    ) -> None:
        """An untagged tool fails closed, so catch the misregistration here.

        Inspects the server-side registry rather than the client-visible tool,
        because tags are a server-side concept and are not sent to clients.
        """
        server = server_for(PermissionLevel.DELETE)
        for tool in await server.list_tools():
            level = required_level(tool.tags)
            assert level is not None, f"{tool.name} has no perm: tag"
            assert TOOL_LEVELS[tool.name] == level, (
                f"{tool.name} is tagged {level.name.lower()} but TOOL_LEVELS "
                f"expects {TOOL_LEVELS[tool.name].name.lower()}"
            )


class TestCallTimeEnforcement:
    """The second layer: refusal must not depend on the tool having been hidden."""

    @pytest.mark.parametrize("level", ALL_LEVELS, ids=lambda lv: lv.name.lower())
    async def test_hidden_tools_are_refused_when_invoked_directly(
        self,
        mcp_client_for: Callable[..., Client],
        fake: FakeSimpleLogin,
        level: PermissionLevel,
    ) -> None:
        forbidden = set(TOOL_LEVELS) - expected_tools(level)
        if not forbidden:
            pytest.skip(f"{level.name.lower()} permits every tool")

        async with mcp_client_for(level) as client:
            for name in sorted(forbidden):
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool(name, TOOL_ARGS[name])

                message = str(excinfo.value)
                # Must be a permission refusal, not an incidental failure such as
                # "unknown tool" or a validation error, which would mean the test
                # passes for the wrong reason.
                assert "requires permission level" in message, message
                assert TOOL_LEVELS[name].name.lower() in message
                assert level.name.lower() in message

    @pytest.mark.parametrize("level", ALL_LEVELS, ids=lambda lv: lv.name.lower())
    async def test_refusal_happens_before_any_upstream_request(
        self,
        mcp_client_for: Callable[..., Client],
        fake: FakeSimpleLogin,
        level: PermissionLevel,
    ) -> None:
        """A refused tool must not touch SimpleLogin at all."""
        forbidden = set(TOOL_LEVELS) - expected_tools(level)
        if not forbidden:
            pytest.skip(f"{level.name.lower()} permits every tool")

        async with mcp_client_for(level) as client:
            fake.request_log.clear()
            for name in sorted(forbidden):
                with pytest.raises(ToolError):
                    await client.call_tool(name, TOOL_ARGS[name])

        assert fake.request_log == [], (
            "a refused tool reached the SimpleLogin API: " f"{fake.request_log}"
        )

    @pytest.mark.parametrize("level", ALL_LEVELS, ids=lambda lv: lv.name.lower())
    async def test_permitted_tools_are_callable(
        self, mcp_client_for: Callable[..., Client], level: PermissionLevel
    ) -> None:
        async with mcp_client_for(level) as client:
            for name in sorted(expected_tools(level)):
                result = await client.call_tool(name, TOOL_ARGS[name])
                assert result.data is not None or result.content is not None

    async def test_unknown_tool_is_distinguishable_from_a_refusal(
        self, mcp_client_for: Callable[..., Client]
    ) -> None:
        """Refusing and not-existing must not be conflated in either direction."""
        async with mcp_client_for(PermissionLevel.READ) as client:
            with pytest.raises(ToolError) as unknown:
                await client.call_tool("delete_alias", {"alias_id": SEEDED_ALIAS_ID})
            with pytest.raises(ToolError) as refused:
                await client.call_tool("toggle_alias", {"alias_id": SEEDED_ALIAS_ID})

        assert "requires permission level" not in str(unknown.value)
        assert "requires permission level" in str(refused.value)


class TestDeleteLevel:
    """Delete is defined but deliberately grants nothing."""

    async def test_delete_grants_nothing_beyond_update(
        self, mcp_client_for: Callable[..., Client]
    ) -> None:
        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            at_update = {tool.name for tool in await client.list_tools()}
        async with mcp_client_for(PermissionLevel.DELETE) as client:
            at_delete = {tool.name for tool in await client.list_tools()}

        assert at_delete == at_update, (
            "Delete has started granting extra tools. If a destructive tool was "
            "added deliberately, this test and PROJECT_GOALS.md both need updating."
        )

    async def test_no_tool_requires_delete(self) -> None:
        assert PermissionLevel.DELETE not in TOOL_LEVELS.values()

    @pytest.mark.parametrize("level", ALL_LEVELS, ids=lambda lv: lv.name.lower())
    async def test_server_never_issues_a_delete_request(
        self,
        mcp_client_for: Callable[..., Client],
        fake: FakeSimpleLogin,
        level: PermissionLevel,
    ) -> None:
        """Exercise every permitted tool and assert no DELETE ever goes upstream.

        The fake implements DELETE, so reaching it would be recorded. This is the
        end-to-end form of "the client has no delete method".
        """
        async with mcp_client_for(level) as client:
            for name in sorted(expected_tools(level)):
                await client.call_tool(name, TOOL_ARGS[name])

        methods = {method for method, _ in fake.request_log}
        assert "DELETE" not in methods, fake.request_log
        assert fake.deleted_alias_ids == []
