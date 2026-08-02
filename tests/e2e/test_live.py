"""End-to-end tests against the real SimpleLogin API, through the built image.

This is the strongest configuration available: the container that would ship,
talking to the live service.

The suite is shaped around a measured constraint. SimpleLogin rate limits alias
*creation* hard -- an exhausted window was observed still refusing after six
minutes idle -- while reads, PATCH and toggle are unaffected. Creating an alias
per test therefore throttles the suite out within a single run. Instead one
durable fixture alias is shared across runs, and only the two tests that
genuinely exercise creation create anything.

Everything ephemeral is stamped with the run id and removed in a ``finally``
block, so a mid-test failure still cleans up. Skipped entirely when
``SI_API_TEST_KEY`` is absent, which is what happens on forked pull requests.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from .live_harness import (
    LiveAccount,
    RateLimited,
    call_with_retry,
    free_signed_suffix,
    is_ours,
    note_for,
    run_id,
)

pytestmark = [pytest.mark.live, pytest.mark.image]

API_KEY = os.environ.get("SI_API_TEST_KEY")

requires_credentials = pytest.mark.skipif(
    not API_KEY,
    reason="SI_API_TEST_KEY is not set (expected on forked pull requests)",
)


@pytest.fixture(scope="module")
def account() -> Iterator[LiveAccount]:
    if not API_KEY:
        pytest.skip("SI_API_TEST_KEY is not set")
    live = LiveAccount(API_KEY)
    try:
        # Only two aliases are ever created per run, plus headroom.
        live.preflight(needed=2)
        yield live
    finally:
        live.sweep()
        live.close()


@pytest.fixture(scope="module")
def shared_alias(account: LiveAccount) -> dict[str, Any]:
    """The durable alias reused by every test that does not test creation."""
    try:
        return account.ensure_persistent_alias()
    except RateLimited as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def can_create_reverse_alias(account: LiveAccount) -> bool:
    """SimpleLogin reports this directly, which beats inferring it from the plan."""
    return bool(account.user_info().get("can_create_reverse_alias"))


@pytest.fixture
def run(account: LiveAccount) -> Iterator[str]:
    """A run-scoped stamp, with teardown of everything carrying it."""
    identifier = run_id()
    try:
        yield identifier
    finally:
        account.sweep(only_run=identifier)


@pytest.fixture
def live_server(run_container: Callable[..., tuple[str, int]]) -> Callable[[str], str]:
    """Start the image pointed at the real SimpleLogin, at a given level."""

    def _start(level: str = "read") -> str:
        _id, port = run_container(
            SIMPLELOGIN_API_KEY=API_KEY or "",
            SIMPLELOGIN_PERMISSION_LEVEL=level,
        )
        return f"http://127.0.0.1:{port}/mcp"

    return _start


@requires_credentials
class TestReadAgainstLiveAccount:
    async def test_reads_succeed_and_mutations_are_refused(
        self, live_server: Callable[[str], str], account: LiveAccount
    ) -> None:
        before = {a["id"] for a in account.all_aliases()}
        url = live_server("read")

        async with Client(url) as client:
            listed = (await client.call_tool("list_aliases", {})).data
            assert "aliases" in listed

            options = (await client.call_tool("get_alias_options", {})).data
            assert free_signed_suffix(options), "no free-tier suffix offered"

            mailboxes = (await client.call_tool("list_mailboxes", {})).data
            assert mailboxes["mailboxes"], "account must have at least one mailbox"

            for tool, args in [
                ("create_random_alias", {}),
                ("toggle_alias", {"alias_id": 1}),
                ("update_alias", {"alias_id": 1, "note": "x"}),
            ]:
                with pytest.raises(ToolError, match="requires permission level"):
                    await client.call_tool(tool, args)

        # A read deployment changed nothing on the real account.
        assert {a["id"] for a in account.all_aliases()} == before

    async def test_pre_existing_account_data_is_untouched(
        self, live_server: Callable[[str], str], account: LiveAccount
    ) -> None:
        """Aliases the suite did not create must survive it exactly.

        SimpleLogin seeds every account with a newsletter alias; that alias is
        not ours to modify or remove.
        """
        foreign = [a for a in account.all_aliases() if not is_ours(a)]
        assert foreign, "expected at least the account's own newsletter alias"

        url = live_server("read")
        async with Client(url) as client:
            await client.call_tool("list_aliases", {})

        after = {a["id"]: a for a in account.all_aliases()}
        for alias in foreign:
            assert alias["id"] in after, f"pre-existing alias {alias['email']} vanished"
            assert after[alias["id"]]["enabled"] == alias["enabled"]
            assert after[alias["id"]]["note"] == alias["note"]

    async def test_reads_the_shared_alias_in_detail(
        self, live_server: Callable[[str], str], shared_alias: dict[str, Any]
    ) -> None:
        url = live_server("read")

        async with Client(url) as client:
            fetched = (
                await client.call_tool("get_alias", {"alias_id": shared_alias["id"]})
            ).data
            assert fetched["email"] == shared_alias["email"]

            activities = (
                await client.call_tool(
                    "get_alias_activities", {"alias_id": shared_alias["id"]}
                )
            ).data
            assert "activities" in activities

            contacts = (
                await client.call_tool(
                    "list_alias_contacts", {"alias_id": shared_alias["id"]}
                )
            ).data
            assert "contacts" in contacts


@requires_credentials
class TestCreateAgainstLiveAccount:
    """The only tests that create. Skipped, not failed, when throttled."""

    async def test_random_alias_round_trip(
        self, live_server: Callable[[str], str], account: LiveAccount, run: str
    ) -> None:
        url = live_server("create")

        async with Client(url) as client:
            try:
                created = await call_with_retry(
                    client,
                    "create_random_alias",
                    {"note": note_for(run, "random round trip")},
                )
            except RateLimited as exc:
                pytest.skip(f"SimpleLogin is throttling alias creation: {exc}")

            assert created["enabled"] is True
            fetched = (
                await client.call_tool("get_alias", {"alias_id": created["id"]})
            ).data
            assert fetched["email"] == created["email"]
            assert run in fetched["note"]

        # Confirmed independently of the server under test.
        assert account.wait_for_alias(created["id"])["email"] == created["email"]

    async def test_custom_alias_uses_a_free_tier_suffix(
        self, live_server: Callable[[str], str], account: LiveAccount, run: str
    ) -> None:
        """Custom addresses are reserved permanently, so this creates exactly one."""
        url = live_server("create")

        async with Client(url) as client:
            options = (
                await client.call_tool("get_alias_options", {"hostname": "example.com"})
            ).data
            if not options["can_create"]:
                pytest.skip("account reports it cannot currently create aliases")

            mailboxes = (await client.call_tool("list_mailboxes", {})).data
            try:
                created = await call_with_retry(
                    client,
                    "create_custom_alias",
                    {
                        "alias_prefix": f"e2e-{run}",
                        "signed_suffix": free_signed_suffix(options),
                        "mailbox_ids": [mailboxes["mailboxes"][0]["id"]],
                        "note": note_for(run, "custom alias"),
                    },
                )
            except RateLimited as exc:
                pytest.skip(f"SimpleLogin is throttling alias creation: {exc}")

        assert created["email"].startswith(f"e2e-{run}")
        assert account.wait_for_alias(created["id"])


@requires_credentials
class TestPermissionBoundariesAgainstLiveAccount:
    async def test_create_level_cannot_amend(
        self,
        live_server: Callable[[str], str],
        account: LiveAccount,
        shared_alias: dict[str, Any],
    ) -> None:
        """Append-only really is append-only, against the real service.

        Refusal happens before any request, so the shared alias is a safe target.
        """
        before = account.get_alias(shared_alias["id"])
        assert before is not None
        url = live_server("create")

        async with Client(url) as client:
            for tool, args in [
                ("update_alias", {"alias_id": shared_alias["id"], "note": "changed"}),
                ("toggle_alias", {"alias_id": shared_alias["id"]}),
            ]:
                with pytest.raises(ToolError, match="requires permission level"):
                    await client.call_tool(tool, args)

        after = account.get_alias(shared_alias["id"])
        assert after is not None
        assert after["note"] == before["note"]
        assert after["enabled"] == before["enabled"]


@requires_credentials
class TestUpdateAgainstLiveAccount:
    """Mutations that do not create, so they are not subject to the throttle."""

    async def test_amend_and_restore_the_shared_alias(
        self,
        live_server: Callable[[str], str],
        account: LiveAccount,
        shared_alias: dict[str, Any],
        run: str,
    ) -> None:
        alias_id = shared_alias["id"]
        original = account.get_alias(alias_id)
        assert original is not None
        url = live_server("update")

        try:
            async with Client(url) as client:
                await client.call_tool(
                    "update_alias",
                    {
                        "alias_id": alias_id,
                        "note": note_for(run, "amended"),
                        "name": "E2E Lifecycle",
                        "pinned": True,
                    },
                )
                amended = (
                    await client.call_tool("get_alias", {"alias_id": alias_id})
                ).data
                assert amended["name"] == "E2E Lifecycle"
                assert amended["pinned"] is True
                assert "amended" in amended["note"]

                # Verified independently of the server under test.
                assert account.get_alias(alias_id)["pinned"] is True
        finally:
            # Restore, so the shared fixture is left as it was found.
            account.restore_alias(alias_id, original)

        restored = account.get_alias(alias_id)
        assert restored is not None
        assert restored["pinned"] == original["pinned"]
        assert restored["name"] == original["name"]

    async def test_toggle_disables_and_re_enables(
        self,
        live_server: Callable[[str], str],
        account: LiveAccount,
        shared_alias: dict[str, Any],
    ) -> None:
        """Disabling must be reversible: it is the non-destructive retirement path."""
        alias_id = shared_alias["id"]
        was_enabled = account.get_alias(alias_id)["enabled"]
        url = live_server("update")

        try:
            async with Client(url) as client:
                first = (
                    await client.call_tool("toggle_alias", {"alias_id": alias_id})
                ).data
                assert first["enabled"] is not was_enabled
                assert account.get_alias(alias_id)["enabled"] is not was_enabled

                second = (
                    await client.call_tool("toggle_alias", {"alias_id": alias_id})
                ).data
                assert second["enabled"] is was_enabled
        finally:
            if account.get_alias(alias_id)["enabled"] is not was_enabled:
                account.set_enabled(alias_id, was_enabled)

        # The alias itself is untouched by all that toggling.
        assert account.get_alias(alias_id) is not None

    async def test_search_finds_the_shared_alias(
        self, live_server: Callable[[str], str], shared_alias: dict[str, Any]
    ) -> None:
        url = live_server("read")

        async with Client(url) as client:
            found = (
                await client.call_tool(
                    "search_aliases", {"query": shared_alias["email"].split("@")[0]}
                )
            ).data

        assert shared_alias["id"] in {a["id"] for a in found["aliases"]}


@requires_credentials
class TestContactsAgainstLiveAccount:
    async def test_contact_creation(
        self,
        live_server: Callable[[str], str],
        shared_alias: dict[str, Any],
        can_create_reverse_alias: bool,
    ) -> None:
        """Reverse aliases are premium-gated; skip rather than fail on a free plan."""
        if not can_create_reverse_alias:
            pytest.skip(
                "account cannot create reverse aliases (free plan); SimpleLogin "
                "gates this behind a paid subscription"
            )

        url = live_server("create")
        address = "e2e-vendor@example.com"

        async with Client(url) as client:
            contact = (
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": shared_alias["id"], "contact": address},
                )
            ).data
            assert contact["reverse_alias_address"]

            # Re-adding is reported as pre-existing rather than duplicated, which
            # also makes this test safe to run repeatedly against a shared alias.
            again = (
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": shared_alias["id"], "contact": address},
                )
            ).data
            assert again["existed"] is True

            listed = (
                await client.call_tool(
                    "list_alias_contacts", {"alias_id": shared_alias["id"]}
                )
            ).data

        assert address in {c["contact"] for c in listed["contacts"]}

    async def test_free_plan_gets_a_clear_upgrade_message(
        self,
        live_server: Callable[[str], str],
        shared_alias: dict[str, Any],
        can_create_reverse_alias: bool,
    ) -> None:
        """The inverse case: on a free plan the gate must be explained, not opaque."""
        if can_create_reverse_alias:
            pytest.skip("account can create reverse aliases; nothing to assert here")

        url = live_server("create")

        async with Client(url) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool(
                    "create_alias_contact",
                    {"alias_id": shared_alias["id"], "contact": "e2e@example.com"},
                )

        assert "upgrade" in str(excinfo.value).lower()


@requires_credentials
class TestNoDestructionAgainstLiveAccount:
    async def test_no_level_can_delete_a_live_alias(
        self,
        live_server: Callable[[str], str],
        account: LiveAccount,
        shared_alias: dict[str, Any],
    ) -> None:
        """The safety property that matters most, verified against real data."""
        url = live_server("delete")

        async with Client(url) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert not [n for n in names if "delete" in n or "remove" in n]

            for attempt in ("delete_alias", "remove_alias", "alias_delete"):
                with pytest.raises(ToolError):
                    await client.call_tool(attempt, {"alias_id": shared_alias["id"]})

        assert account.get_alias(shared_alias["id"]) is not None

    async def test_every_level_leaves_the_account_intact(
        self, live_server: Callable[[str], str], account: LiveAccount
    ) -> None:
        """Exercise each level end to end and confirm nothing disappeared."""
        before = {a["id"] for a in account.all_aliases()}

        for level in ("read", "create", "update", "delete"):
            url = live_server(level)
            async with Client(url) as client:
                await client.call_tool("list_aliases", {})
                await client.call_tool("list_mailboxes", {})

        assert {a["id"] for a in account.all_aliases()} >= before
