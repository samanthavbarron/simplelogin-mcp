"""Multi-step workflows through the MCP layer, against the stateful fake.

These mirror what an agent actually does -- discover, create, inspect, amend --
rather than exercising endpoints in isolation, and they run without network
access so forked pull requests can execute them.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from simplelogin_mcp.permissions import PermissionLevel

from .fake_simplelogin import FakeSimpleLogin


def free_suffix(options: dict) -> str:
    """Pick a suffix a free account may actually use.

    Selecting the first suffix would silently pass on a premium account and fail
    once the plan lapses.
    """
    for entry in options["suffixes"]:
        if not entry["is_premium"]:
            return entry["signed_suffix"]
    raise AssertionError("no free suffix offered")


class TestAliasLifecycle:
    async def test_discover_create_amend_and_disable(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            options = (
                await client.call_tool("get_alias_options", {"hostname": "example.com"})
            ).data
            assert options["can_create"] is True
            assert options["prefix_suggestion"] == "example"

            mailboxes = (await client.call_tool("list_mailboxes", {})).data
            mailbox_id = mailboxes["mailboxes"][0]["id"]

            created = (
                await client.call_tool(
                    "create_custom_alias",
                    {
                        "alias_prefix": "shopping",
                        "signed_suffix": free_suffix(options),
                        "mailbox_ids": [mailbox_id],
                        "note": "for online shops",
                        "hostname": "example.com",
                    },
                )
            ).data
            alias_id = created["id"]
            assert created["email"].startswith("shopping.")
            assert created["enabled"] is True

            fetched = (await client.call_tool("get_alias", {"alias_id": alias_id})).data
            assert fetched["note"] == "for online shops"

            await client.call_tool(
                "update_alias",
                {"alias_id": alias_id, "note": "updated note", "name": "Shopping"},
            )
            amended = (await client.call_tool("get_alias", {"alias_id": alias_id})).data
            assert amended["note"] == "updated note"
            assert amended["name"] == "Shopping"

            toggled = (
                await client.call_tool("toggle_alias", {"alias_id": alias_id})
            ).data
            assert toggled["enabled"] is False

            disabled = (
                await client.call_tool("list_aliases", {"filter_by": "disabled"})
            ).data
            assert [a["id"] for a in disabled["aliases"]] == [alias_id]

            restored = (
                await client.call_tool("toggle_alias", {"alias_id": alias_id})
            ).data
            assert restored["enabled"] is True

        # The alias survived the whole workflow: disabling is not deletion.
        assert alias_id in fake.aliases
        assert fake.deleted_alias_ids == []

    async def test_random_alias_then_contacts(
        self, mcp_client_for: Callable[..., Client]
    ) -> None:
        async with mcp_client_for(PermissionLevel.CREATE) as client:
            alias = (
                await client.call_tool("create_random_alias", {"note": "throwaway"})
            ).data
            alias_id = alias["id"]

            contact = (
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": alias_id, "contact": "vendor@example.com"},
                )
            ).data
            assert contact["existed"] is False
            assert contact["reverse_alias_address"]

            listed = (
                await client.call_tool("list_alias_contacts", {"alias_id": alias_id})
            ).data
            assert [c["contact"] for c in listed["contacts"]] == ["vendor@example.com"]

            # Re-adding is idempotent and reported as such rather than duplicating.
            again = (
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": alias_id, "contact": "vendor@example.com"},
                )
            ).data
            assert again["existed"] is True

            still = (
                await client.call_tool("list_alias_contacts", {"alias_id": alias_id})
            ).data
            assert len(still["contacts"]) == 1

    async def test_search_finds_alias_by_note(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        fake.add_alias(email="banking@aleeas.com", note="finance stuff")
        fake.add_alias(email="social@aleeas.com", note="friends")

        async with mcp_client_for(PermissionLevel.READ) as client:
            found = (await client.call_tool("search_aliases", {"query": "finance"})).data

        assert [a["email"] for a in found["aliases"]] == ["banking@aleeas.com"]


class TestPaginationThroughTools:
    async def test_auto_pagination_returns_everything(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        for index in range(45):
            fake.add_alias(email=f"bulk{index}@aleeas.com")

        async with mcp_client_for(PermissionLevel.READ) as client:
            result = (await client.call_tool("list_aliases", {})).data

        assert len(result["aliases"]) == 45
        assert result["has_more"] is False

    async def test_explicit_paging_walks_pages(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        for index in range(25):
            fake.add_alias(email=f"bulk{index}@aleeas.com")

        async with mcp_client_for(PermissionLevel.READ) as client:
            page0 = (await client.call_tool("list_aliases", {"page_id": 0})).data
            page1 = (await client.call_tool("list_aliases", {"page_id": 1})).data

        assert len(page0["aliases"]) == 20
        assert page0["has_more"] is True
        assert len(page1["aliases"]) == 5
        assert page1["has_more"] is False

        seen = {a["id"] for a in page0["aliases"]} | {a["id"] for a in page1["aliases"]}
        assert len(seen) == 25, "pages overlapped or skipped entries"

    async def test_auto_pagination_cap_is_reported(
        self, mcp_client_for_fake: Callable[..., Client]
    ) -> None:
        """Truncation must surface as has_more rather than looking complete."""
        fake = FakeSimpleLogin()
        for index in range(60):
            fake.add_alias(email=f"bulk{index}@aleeas.com")

        async with mcp_client_for_fake(
            fake, PermissionLevel.READ, SIMPLELOGIN_MAX_AUTO_PAGES=2
        ) as client:
            result = (await client.call_tool("list_aliases", {})).data

        assert len(result["aliases"]) == 40
        assert result["has_more"] is True


class TestPlanLimitsSurfaceClearly:
    async def test_free_account_gets_the_upgrade_message_for_contacts(
        self, mcp_client_for_fake: Callable[..., Client]
    ) -> None:
        fake = FakeSimpleLogin(is_premium=False, can_create_reverse_alias=False)
        alias = fake.add_alias()

        async with mcp_client_for_fake(fake, PermissionLevel.CREATE) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": alias["id"], "contact": "x@example.com"},
                )

        assert "upgrade" in str(excinfo.value).lower()

    async def test_quota_exhaustion_is_reported_from_options_and_creation(
        self, mcp_client_for_fake: Callable[..., Client]
    ) -> None:
        fake = FakeSimpleLogin(
            is_premium=False, enforce_quota=True, max_alias_free_plan=2
        )
        fake.add_alias(email="one@aleeas.com")
        fake.add_alias(email="two@aleeas.com")

        async with mcp_client_for_fake(fake, PermissionLevel.CREATE) as client:
            options = (await client.call_tool("get_alias_options", {})).data
            assert options["can_create"] is False

            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("create_random_alias", {})

        assert "limitation of a free account" in str(excinfo.value)


class TestPermissionBoundariesMidWorkflow:
    async def test_create_level_can_build_but_not_amend(
        self, mcp_client_for: Callable[..., Client]
    ) -> None:
        """An append-only deployment: it can make an alias but never change it."""
        async with mcp_client_for(PermissionLevel.CREATE) as client:
            options = (await client.call_tool("get_alias_options", {})).data
            mailboxes = (await client.call_tool("list_mailboxes", {})).data

            created = (
                await client.call_tool(
                    "create_custom_alias",
                    {
                        "alias_prefix": "appendonly",
                        "signed_suffix": free_suffix(options),
                        "mailbox_ids": [mailboxes["mailboxes"][0]["id"]],
                    },
                )
            ).data
            alias_id = created["id"]

            # Reading back its own work is allowed.
            assert (await client.call_tool("get_alias", {"alias_id": alias_id})).data[
                "id"
            ] == alias_id

            for tool, args in [
                ("update_alias", {"alias_id": alias_id, "note": "nope"}),
                ("toggle_alias", {"alias_id": alias_id}),
            ]:
                with pytest.raises(ToolError, match="requires permission level"):
                    await client.call_tool(tool, args)

    async def test_read_level_cannot_change_anything(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="readonly@aleeas.com", note="original")

        async with mcp_client_for(PermissionLevel.READ) as client:
            assert (await client.call_tool("list_aliases", {})).data["aliases"]

            for tool, args in [
                ("create_random_alias", {}),
                ("update_alias", {"alias_id": alias["id"], "note": "changed"}),
                ("toggle_alias", {"alias_id": alias["id"]}),
            ]:
                with pytest.raises(ToolError, match="requires permission level"):
                    await client.call_tool(tool, args)

        # State is byte-for-byte untouched.
        assert fake.aliases[alias["id"]]["note"] == "original"
        assert fake.aliases[alias["id"]]["enabled"] is True
        assert len(fake.aliases) == 1

    async def test_update_level_completes_the_full_workflow(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            alias = (await client.call_tool("create_random_alias", {})).data
            await client.call_tool(
                "update_alias", {"alias_id": alias["id"], "pinned": True}
            )
            await client.call_tool("toggle_alias", {"alias_id": alias["id"]})

            pinned = (
                await client.call_tool("list_aliases", {"filter_by": "pinned"})
            ).data

        assert [a["id"] for a in pinned["aliases"]] == [alias["id"]]
        assert fake.deleted_alias_ids == []
