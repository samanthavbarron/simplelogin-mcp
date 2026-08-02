"""Async HTTP client for the SimpleLogin alias API.

Deliberately has no ``delete`` method. Alias deletion is out of scope for this
server (see PROJECT_GOALS.md), and omitting it from the client rather than
merely from the tool layer means the shipped code has no path to it at all.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from .errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    SimpleLoginError,
    UpgradeRequiredError,
)

#: SimpleLogin returns at most this many items per page on every list endpoint.
PAGE_SIZE = 20


class SimpleLoginClient:
    """Thin, typed wrapper over the subset of endpoints this server exposes."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://app.simplelogin.io",
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authentication": api_key},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> SimpleLoginClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_json = None if json is None else {k: v for k, v in json.items() if v is not None}

        try:
            response = await self._http.request(
                method, path, params=clean_params or None, json=clean_json
            )
        except httpx.TimeoutException as exc:
            raise SimpleLoginError(f"SimpleLogin request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SimpleLoginError(f"Could not reach SimpleLogin: {exc}") from exc

        if response.is_success:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise SimpleLoginError(
                    f"SimpleLogin returned a non-JSON body (HTTP {response.status_code})"
                ) from exc

        raise self._to_error(response)

    @staticmethod
    def _to_error(response: httpx.Response) -> SimpleLoginError:
        # SimpleLogin's documented error shape is {"error": "..."}. Its text is
        # user-facing and worth preserving verbatim -- quota and upgrade
        # messages in particular explain themselves better than we could.
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or body.get("message") or "")
        except ValueError:
            detail = response.text[:200]

        status = response.status_code
        message = detail or f"SimpleLogin returned HTTP {status}"

        if status == 401:
            return AuthenticationError(
                f"SimpleLogin rejected the API key: {message}", status_code=status
            )
        if status == 403:
            return UpgradeRequiredError(message, status_code=status)
        if status == 404:
            return NotFoundError(message, status_code=status)
        if status == 429:
            return RateLimitedError(
                f"SimpleLogin is rate limiting this account: {message}", status_code=status
            )
        return SimpleLoginError(message, status_code=status)

    async def _paginate(
        self,
        path: str,
        key: str,
        *,
        page_id: int | None,
        max_pages: int,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch one explicit page, or walk pages up to ``max_pages``.

        Returns the item list plus ``has_more`` so a caller that let us
        auto-paginate can still tell it hit the cap.
        """
        if page_id is not None:
            payload = await self._request(
                "GET", path, params={**(params or {}), "page_id": page_id}
            )
            items = payload.get(key, []) if isinstance(payload, dict) else []
            return {
                key: items,
                "page_id": page_id,
                "has_more": len(items) == PAGE_SIZE,
            }

        collected: list[Any] = []
        page = 0
        has_more = False
        while page < max_pages:
            payload = await self._request(
                "GET", path, params={**(params or {}), "page_id": page}
            )
            items = payload.get(key, []) if isinstance(payload, dict) else []
            collected.extend(items)
            if len(items) < PAGE_SIZE:
                break
            page += 1
            # Ran out of budget with a full page in hand: more probably remain.
            if page >= max_pages:
                has_more = True
        return {key: collected, "pages_fetched": page + 1, "has_more": has_more}

    # ------------------------------------------------------------------ aliases

    async def get_alias_options(self, hostname: str | None = None) -> Any:
        return await self._request(
            "GET", "/api/v5/alias/options", params={"hostname": hostname}
        )

    async def create_custom_alias(
        self,
        *,
        alias_prefix: str,
        signed_suffix: str,
        mailbox_ids: list[int],
        note: str | None = None,
        name: str | None = None,
        hostname: str | None = None,
    ) -> Any:
        return await self._request(
            "POST",
            "/api/v3/alias/custom/new",
            params={"hostname": hostname},
            json={
                "alias_prefix": alias_prefix,
                "signed_suffix": signed_suffix,
                "mailbox_ids": mailbox_ids,
                "note": note,
                "name": name,
            },
        )

    async def create_random_alias(
        self, *, note: str | None = None, hostname: str | None = None
    ) -> Any:
        # `mode` is intentionally not plumbed through: random alias style follows
        # the account's own setting.
        return await self._request(
            "POST",
            "/api/alias/random/new",
            params={"hostname": hostname},
            json={"note": note},
        )

    async def list_aliases(
        self,
        *,
        page_id: int | None = None,
        max_pages: int = 10,
        filter_by: Literal["pinned", "disabled", "enabled"] | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if filter_by is not None:
            # The API treats pinned/disabled/enabled as mutually exclusive flags
            # whose presence alone is the signal.
            params[filter_by] = True

        if query is not None:
            # Search needs a body, which is unreliable on GET; the API accepts
            # POST for exactly this case.
            payload = await self._request(
                "POST",
                "/api/v2/aliases",
                params={**params, "page_id": page_id or 0},
                json={"query": query},
            )
            items = payload.get("aliases", []) if isinstance(payload, dict) else []
            return {
                "aliases": items,
                "page_id": page_id or 0,
                "has_more": len(items) == PAGE_SIZE,
            }

        return await self._paginate(
            "/api/v2/aliases",
            "aliases",
            page_id=page_id,
            max_pages=max_pages,
            params=params,
        )

    async def get_alias(self, alias_id: int) -> Any:
        return await self._request("GET", f"/api/aliases/{alias_id}")

    async def toggle_alias(self, alias_id: int) -> Any:
        return await self._request("POST", f"/api/aliases/{alias_id}/toggle")

    async def get_alias_activities(
        self, alias_id: int, *, page_id: int | None = None, max_pages: int = 10
    ) -> dict[str, Any]:
        return await self._paginate(
            f"/api/aliases/{alias_id}/activities",
            "activities",
            page_id=page_id,
            max_pages=max_pages,
        )

    async def update_alias(
        self,
        alias_id: int,
        *,
        note: str | None = None,
        name: str | None = None,
        mailbox_ids: list[int] | None = None,
        disable_pgp: bool | None = None,
        pinned: bool | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "note": note,
            "name": name,
            "mailbox_ids": mailbox_ids,
            "disable_pgp": disable_pgp,
            "pinned": pinned,
        }
        if all(v is None for v in body.values()):
            raise SimpleLoginError(
                "update_alias needs at least one field to change "
                "(note, name, mailbox_ids, disable_pgp or pinned)."
            )
        result = await self._request("PATCH", f"/api/aliases/{alias_id}", json=body)
        # PATCH returns a bare 200 with no useful body; echo what changed so the
        # caller gets a meaningful result instead of null.
        return result if result else {
            "ok": True,
            "alias_id": alias_id,
            "updated": [k for k, v in body.items() if v is not None],
        }

    # ----------------------------------------------------------------- contacts

    async def list_alias_contacts(
        self, alias_id: int, *, page_id: int | None = None, max_pages: int = 10
    ) -> dict[str, Any]:
        return await self._paginate(
            f"/api/aliases/{alias_id}/contacts",
            "contacts",
            page_id=page_id,
            max_pages=max_pages,
        )

    async def create_alias_contact(self, alias_id: int, contact: str) -> Any:
        return await self._request(
            "POST", f"/api/aliases/{alias_id}/contacts", json={"contact": contact}
        )

    # ---------------------------------------------------------------- mailboxes

    async def list_mailboxes(self) -> Any:
        """Read-only, and in scope only because alias creation requires it.

        ``POST /api/v3/alias/custom/new`` takes ``mailbox_ids`` and there is no
        other way to discover a valid ID on an account with no aliases yet.
        """
        return await self._request("GET", "/api/v2/mailboxes")
