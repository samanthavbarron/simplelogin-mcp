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


class TestContactBlocking:
    """Blocking one sender without touching the alias.

    The motivating case: a spammy sender on an otherwise useful alias. The
    caller arrives with an alias id and a sender address from a mail header,
    not a contact id.
    """

    async def test_blocks_by_sender_address_and_is_reversible(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="news@aleeas.com")
        spam = fake.add_contact(alias["id"], "no-reply@is.email.nextdoor.com")
        keep = fake.add_contact(alias["id"], "friend@example.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            blocked = (
                await client.call_tool(
                    "toggle_contact_block",
                    {
                        "alias_id": alias["id"],
                        "contact": "no-reply@is.email.nextdoor.com",
                    },
                )
            ).data
            assert blocked["block_forward"] is True
            assert blocked["contact_id"] == spam["id"]

            # Unblocking is the same call again.
            restored = (
                await client.call_tool(
                    "toggle_contact_block",
                    {"alias_id": alias["id"], "contact_id": spam["id"]},
                )
            ).data
            assert restored["block_forward"] is False

        # The alias itself and every other contact are untouched throughout.
        assert fake.aliases[alias["id"]]["enabled"] is True
        assert keep["block_forward"] is False

    async def test_resolves_by_reverse_alias_address(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        """Mail headers carry the reverse-alias, not the real sender address."""
        alias = fake.add_alias(email="news@aleeas.com")
        spam = fake.add_contact(alias["id"], "no-reply@is.email.nextdoor.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            result = (
                await client.call_tool(
                    "toggle_contact_block",
                    {
                        "alias_id": alias["id"],
                        "contact": spam["reverse_alias_address"].upper(),
                    },
                )
            ).data

        assert result["contact_id"] == spam["id"]
        assert result["block_forward"] is True

    async def test_will_not_act_on_a_contact_from_another_alias(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        """The safety property: a mistaken id must fail, not block the wrong sender."""
        mine = fake.add_alias(email="mine@aleeas.com")
        theirs = fake.add_alias(email="theirs@aleeas.com")
        fake.add_contact(mine["id"], "someone@example.com")
        foreign = fake.add_contact(theirs["id"], "victim@example.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            with pytest.raises(ToolError, match="different alias"):
                await client.call_tool(
                    "toggle_contact_block",
                    {"alias_id": mine["id"], "contact_id": foreign["id"]},
                )

        assert foreign["block_forward"] is False

    async def test_unknown_contact_is_reported_clearly(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="news@aleeas.com")
        fake.add_contact(alias["id"], "known@example.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            with pytest.raises(ToolError, match="no contact matching"):
                await client.call_tool(
                    "toggle_contact_block",
                    {"alias_id": alias["id"], "contact": "ghost@example.com"},
                )

    async def test_ambiguous_match_refuses_rather_than_guessing(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        """Two contacts can share a reverse-alias prefix; picking one is wrong."""
        alias = fake.add_alias(email="news@aleeas.com")
        fake.add_contact(alias["id"], "dupe@example.com")
        fake.add_contact(alias["id"], "dupe@example.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            with pytest.raises(ToolError, match="matched 2 contacts"):
                await client.call_tool(
                    "toggle_contact_block",
                    {"alias_id": alias["id"], "contact": "dupe@example.com"},
                )

    async def test_requires_some_way_to_identify_the_contact(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="news@aleeas.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            with pytest.raises(ToolError, match="supply either contact"):
                await client.call_tool(
                    "toggle_contact_block", {"alias_id": alias["id"]}
                )

    async def test_finds_a_contact_beyond_the_first_page(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        """The real motivating case: the sender worth blocking is an old one."""
        alias = fake.add_alias(email="busy@aleeas.com")
        for index in range(45):
            fake.add_contact(alias["id"], f"filler{index}@example.com")
        buried = fake.add_contact(alias["id"], "spammer@example.com")

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            result = (
                await client.call_tool(
                    "toggle_contact_block",
                    {"alias_id": alias["id"], "contact": "spammer@example.com"},
                )
            ).data

        assert result["contact_id"] == buried["id"]
        assert result["block_forward"] is True

    async def test_blocking_needs_update_level(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="news@aleeas.com")
        contact = fake.add_contact(alias["id"], "spam@example.com")

        for level in (PermissionLevel.READ, PermissionLevel.CREATE):
            async with mcp_client_for(level) as client:
                with pytest.raises(ToolError, match="requires permission level"):
                    await client.call_tool(
                        "toggle_contact_block",
                        {"alias_id": alias["id"], "contact": "spam@example.com"},
                    )

        assert contact["block_forward"] is False


class TestUpdateAliasTriState:
    """pinned/disable_pgp are string enums, not optional bools -- see tools.py."""

    async def test_unchanged_leaves_the_field_alone(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="tri@aleeas.com", pinned=True)

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            await client.call_tool(
                "update_alias", {"alias_id": alias["id"], "note": "touched"}
            )

        assert fake.aliases[alias["id"]]["pinned"] is True
        assert fake.aliases[alias["id"]]["note"] == "touched"

    @pytest.mark.parametrize(
        ("value", "expected"), [("true", True), ("false", False)]
    )
    async def test_explicit_values_are_applied(
        self,
        mcp_client_for: Callable[..., Client],
        fake: FakeSimpleLogin,
        value: str,
        expected: bool,
    ) -> None:
        alias = fake.add_alias(email="tri@aleeas.com", pinned=not expected)

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            await client.call_tool(
                "update_alias", {"alias_id": alias["id"], "pinned": value}
            )

        assert fake.aliases[alias["id"]]["pinned"] is expected

    async def test_empty_mailbox_ids_means_unchanged(
        self, mcp_client_for: Callable[..., Client], fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias(email="tri@aleeas.com")
        before = list(alias["mailboxes"])

        async with mcp_client_for(PermissionLevel.UPDATE) as client:
            await client.call_tool(
                "update_alias",
                {"alias_id": alias["id"], "note": "n", "mailbox_ids": []},
            )

        assert fake.aliases[alias["id"]]["mailboxes"] == before


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
                "update_alias", {"alias_id": alias["id"], "pinned": "true"}
            )
            await client.call_tool("toggle_alias", {"alias_id": alias["id"]})

            pinned = (
                await client.call_tool("list_aliases", {"filter_by": "pinned"})
            ).data

        assert [a["id"] for a in pinned["aliases"]] == [alias["id"]]
        assert fake.deleted_alias_ids == []
