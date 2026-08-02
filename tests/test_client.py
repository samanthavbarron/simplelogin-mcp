"""SimpleLogin client behaviour: pagination, error mapping, and what it cannot do."""

from __future__ import annotations

import httpx
import pytest

from simplelogin_mcp.client import PAGE_SIZE, SimpleLoginClient
from simplelogin_mcp.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    SimpleLoginError,
    UpgradeRequiredError,
)

from .fake_simplelogin import FakeSimpleLogin

BASE_URL = "http://simplelogin.test"


def client_for(fake: FakeSimpleLogin, *, api_key: str | None = None) -> SimpleLoginClient:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app()),
        base_url=BASE_URL,
        headers={"Authentication": api_key or fake.api_key},
    )
    return SimpleLoginClient(fake.api_key, base_url=BASE_URL, http_client=http)


class TestPagination:
    async def test_auto_pagination_walks_every_page(
        self, fake: FakeSimpleLogin
    ) -> None:
        for index in range(45):
            fake.add_alias(email=f"alias{index}@aleeas.com")

        result = await client_for(fake).list_aliases()

        assert len(result["aliases"]) == 45
        assert result["has_more"] is False
        assert result["pages_fetched"] == 3

    async def test_explicit_page_returns_one_page(self, fake: FakeSimpleLogin) -> None:
        for index in range(25):
            fake.add_alias(email=f"alias{index}@aleeas.com")

        first = await client_for(fake).list_aliases(page_id=0)
        second = await client_for(fake).list_aliases(page_id=1)

        assert len(first["aliases"]) == PAGE_SIZE
        assert first["has_more"] is True
        assert len(second["aliases"]) == 5
        assert second["has_more"] is False

    async def test_auto_pagination_reports_hitting_the_cap(
        self, fake: FakeSimpleLogin
    ) -> None:
        """Truncation must be visible, not silent."""
        for index in range(60):
            fake.add_alias(email=f"alias{index}@aleeas.com")

        result = await client_for(fake).list_aliases(max_pages=2)

        assert len(result["aliases"]) == 40
        assert result["has_more"] is True

    @pytest.mark.parametrize(
        ("total", "max_pages", "expected_items", "expected_pages"),
        [
            (60, 2, 40, 2),  # cap hit — previously reported 3
            (45, 10, 45, 3),
            (20, 10, 20, 2),  # full page then an empty one
            (0, 10, 0, 1),
            (40, 2, 40, 2),  # cap lands exactly on the last full page
        ],
    )
    async def test_pages_fetched_counts_actual_requests(
        self,
        fake: FakeSimpleLogin,
        total: int,
        max_pages: int,
        expected_items: int,
        expected_pages: int,
    ) -> None:
        """pages_fetched must match reality; it was off by one at the cap."""
        for index in range(total):
            fake.add_alias(email=f"alias{index}@aleeas.com")
        fake.request_log.clear()

        result = await client_for(fake).list_aliases(max_pages=max_pages)

        assert len(result["aliases"]) == expected_items
        assert result["pages_fetched"] == expected_pages
        # The count is only meaningful if it matches the requests actually made.
        assert len(fake.request_log) == expected_pages

    async def test_exactly_one_full_page_is_not_reported_as_truncated(
        self, fake: FakeSimpleLogin
    ) -> None:
        """A boundary that is easy to get wrong: 20 items is a full page but the last."""
        for index in range(PAGE_SIZE):
            fake.add_alias(email=f"alias{index}@aleeas.com")

        result = await client_for(fake).list_aliases()

        assert len(result["aliases"]) == PAGE_SIZE
        assert result["has_more"] is False

    async def test_empty_account_paginates_cleanly(self, fake: FakeSimpleLogin) -> None:
        result = await client_for(fake).list_aliases()
        assert result["aliases"] == []
        assert result["has_more"] is False


class TestErrorMapping:
    async def test_bad_api_key_maps_to_authentication_error(
        self, fake: FakeSimpleLogin
    ) -> None:
        with pytest.raises(AuthenticationError) as excinfo:
            await client_for(fake, api_key="wrong-key").list_aliases()
        assert "Wrong api key" in str(excinfo.value)
        assert excinfo.value.status_code == 401

    async def test_missing_alias_maps_to_not_found(self, fake: FakeSimpleLogin) -> None:
        with pytest.raises(NotFoundError):
            await client_for(fake).get_alias(999_999)

    async def test_upgrade_required_preserves_the_api_message(self) -> None:
        """SimpleLogin's own wording explains the situation better than ours would."""
        fake = FakeSimpleLogin(is_premium=False, can_create_reverse_alias=False)
        alias = fake.add_alias()

        with pytest.raises(UpgradeRequiredError) as excinfo:
            await client_for(fake).create_alias_contact(alias["id"], "x@example.com")

        assert str(excinfo.value) == "Please upgrade to create a reverse-alias"

    async def test_quota_message_is_surfaced_verbatim(self) -> None:
        fake = FakeSimpleLogin(
            is_premium=False, enforce_quota=True, max_alias_free_plan=1
        )
        fake.add_alias()

        with pytest.raises(SimpleLoginError) as excinfo:
            await client_for(fake).create_random_alias()

        assert "reached the limitation of a free account" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthenticationError),
            (403, UpgradeRequiredError),
            (404, NotFoundError),
            (429, RateLimitedError),
            (500, SimpleLoginError),
        ],
    )
    async def test_status_codes_map_to_types(
        self, status: int, expected: type[Exception]
    ) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"error": "boom"})
        )
        http = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        client = SimpleLoginClient("k", base_url=BASE_URL, http_client=http)

        with pytest.raises(expected):
            await client.get_alias(1)

    async def test_network_failure_is_wrapped(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(explode), base_url=BASE_URL
        )
        client = SimpleLoginClient("k", base_url=BASE_URL, http_client=http)

        with pytest.raises(SimpleLoginError, match="Could not reach SimpleLogin"):
            await client.get_alias(1)

    async def test_non_json_success_body_is_reported_clearly(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, text="<html>maintenance</html>")
        )
        http = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        client = SimpleLoginClient("k", base_url=BASE_URL, http_client=http)

        with pytest.raises(SimpleLoginError, match="non-JSON"):
            await client.get_alias(1)


class TestUpdateAlias:
    async def test_rejects_an_empty_update_without_calling_the_api(
        self, fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias()
        fake.request_log.clear()

        with pytest.raises(SimpleLoginError, match="at least one field"):
            await client_for(fake).update_alias(alias["id"])

        assert fake.request_log == []

    async def test_bare_200_is_turned_into_a_useful_result(
        self, fake: FakeSimpleLogin
    ) -> None:
        """The real API returns no body on PATCH; echo what changed instead of null."""
        alias = fake.add_alias()

        result = await client_for(fake).update_alias(
            alias["id"], note="updated", pinned=True
        )

        assert result["ok"] is True
        assert result["alias_id"] == alias["id"]
        assert set(result["updated"]) == {"note", "pinned"}

    async def test_none_fields_are_not_sent(self, fake: FakeSimpleLogin) -> None:
        """Omitted fields must not be transmitted as explicit nulls and wipe data."""
        alias = fake.add_alias(note="keep me", name="Keep Me")

        await client_for(fake).update_alias(alias["id"], pinned=True)

        assert fake.aliases[alias["id"]]["note"] == "keep me"
        assert fake.aliases[alias["id"]]["name"] == "Keep Me"
        assert fake.aliases[alias["id"]]["pinned"] is True


class TestNoDeletePath:
    def test_client_exposes_no_delete_method(self) -> None:
        """The omission is the feature; assert it so it cannot creep back in."""
        suspicious = [
            name
            for name in dir(SimpleLoginClient)
            if not name.startswith("_") and ("delete" in name or "remove" in name)
        ]
        assert suspicious == []

    async def test_no_client_operation_issues_a_delete(
        self, fake: FakeSimpleLogin
    ) -> None:
        alias = fake.add_alias()
        client = client_for(fake)

        await client.get_alias_options()
        await client.list_aliases()
        await client.list_mailboxes()
        await client.get_alias(alias["id"])
        await client.get_alias_activities(alias["id"])
        await client.list_alias_contacts(alias["id"])
        await client.create_random_alias(note="n")
        await client.create_alias_contact(alias["id"], "c@example.com")
        await client.update_alias(alias["id"], note="n")
        await client.toggle_alias(alias["id"])

        assert {method for method, _ in fake.request_log} == {"GET", "POST", "PATCH"}
