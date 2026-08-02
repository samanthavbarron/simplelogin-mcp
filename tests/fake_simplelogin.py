"""A stateful in-memory stand-in for the SimpleLogin API.

Response shapes are modelled on real captured responses from app.simplelogin.io,
including the quirks that matter: 20-item pages, ``is_premium`` suffixes, the
bare-200 PATCH, and the 403 on reverse-alias creation for free accounts.

It also records every request, which lets tests assert a negative that matters
here -- that the server never issues a DELETE under any circumstances.
"""

from __future__ import annotations

import itertools
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PAGE_SIZE = 20

FREE_SUFFIXES = [".raffle542@aleeas.com", ".cacti512@slmails.com"]
PREMIUM_SUFFIXES = [".placard658@simplelogin.com", ".thrash642@8alias.com"]


class FakeSimpleLogin:
    """In-memory SimpleLogin. Construct, call ``.app()``, point a client at it."""

    def __init__(
        self,
        *,
        api_key: str = "test-key",
        is_premium: bool = True,
        can_create_reverse_alias: bool = True,
        max_alias_free_plan: int = 10,
        enforce_quota: bool = False,
    ) -> None:
        self.api_key = api_key
        self.is_premium = is_premium
        self.can_create_reverse_alias = can_create_reverse_alias
        self.max_alias_free_plan = max_alias_free_plan
        self.enforce_quota = enforce_quota

        self.aliases: dict[int, dict[str, Any]] = {}
        self.contacts: dict[int, list[dict[str, Any]]] = {}
        self.activities: dict[int, list[dict[str, Any]]] = {}
        self.mailboxes: list[dict[str, Any]] = [
            {
                "id": 14616811,
                "email": "owner@example.com",
                "default": True,
                "verified": True,
                "creation_timestamp": 1785686159,
                "nb_alias": 0,
            }
        ]

        self.request_log: list[tuple[str, str]] = []
        self.deleted_alias_ids: list[int] = []
        self._alias_ids = itertools.count(47372146)
        self._contact_ids = itertools.count(1)
        self._random_words = itertools.count(1)

    # ------------------------------------------------------------------ helpers

    def add_alias(self, email: str = "seed@aleeas.com", **overrides: Any) -> dict[str, Any]:
        """Seed an alias directly, bypassing the HTTP layer."""
        alias_id = next(self._alias_ids)
        alias = {
            "id": alias_id,
            "email": email,
            "name": None,
            "enabled": True,
            "creation_date": "2026-08-02 15:56:00+00:00",
            "creation_timestamp": 1785686160,
            "note": None,
            "nb_block": 0,
            "nb_forward": 0,
            "nb_reply": 0,
            "support_pgp": False,
            "disable_pgp": False,
            "pinned": False,
            "mailbox": {"id": self.mailboxes[0]["id"], "email": self.mailboxes[0]["email"]},
            "mailboxes": [
                {"id": self.mailboxes[0]["id"], "email": self.mailboxes[0]["email"]}
            ],
            "latest_activity": None,
        }
        alias.update(overrides)
        self.aliases[alias_id] = alias
        self.contacts.setdefault(alias_id, [])
        self.activities.setdefault(alias_id, [])
        return alias

    def _auth_ok(self, request: Request) -> bool:
        return request.headers.get("Authentication") == self.api_key

    @staticmethod
    def _page(items: list[Any], request: Request) -> list[Any]:
        try:
            page = int(request.query_params.get("page_id", 0))
        except ValueError:
            page = 0
        start = page * PAGE_SIZE
        return items[start : start + PAGE_SIZE]

    def _suffixes(self) -> list[dict[str, Any]]:
        entries = []
        for suffix in PREMIUM_SUFFIXES:
            entries.append(
                {
                    "suffix": suffix,
                    "signed_suffix": f"{suffix}.sig-premium",
                    "is_custom": False,
                    "is_premium": True,
                }
            )
        for suffix in FREE_SUFFIXES:
            entries.append(
                {
                    "suffix": suffix,
                    "signed_suffix": f"{suffix}.sig-free",
                    "is_custom": False,
                    "is_premium": False,
                }
            )
        return entries

    def _quota_exceeded(self) -> bool:
        return (
            self.enforce_quota
            and not self.is_premium
            and len(self.aliases) >= self.max_alias_free_plan
        )

    # ------------------------------------------------------------------- routes

    async def _options(self, request: Request) -> JSONResponse:
        hostname = request.query_params.get("hostname", "")
        prefix = hostname.split(".")[0] if hostname else ""
        return JSONResponse(
            {
                "can_create": not self._quota_exceeded(),
                "prefix_suggestion": prefix,
                "suffixes": self._suffixes(),
            }
        )

    async def _create_custom(self, request: Request) -> JSONResponse:
        body = await request.json()
        for field in ("alias_prefix", "signed_suffix", "mailbox_ids"):
            if not body.get(field):
                return JSONResponse({"error": f"{field} is required"}, status_code=400)

        if self._quota_exceeded():
            return JSONResponse(
                {"error": "You have reached the limitation of a free account"},
                status_code=400,
            )

        known = {entry["signed_suffix"] for entry in self._suffixes()}
        signed = body["signed_suffix"]
        if signed not in known:
            return JSONResponse({"error": "Invalid suffix"}, status_code=400)

        suffix = signed.rsplit(".sig-", 1)[0]
        if signed.endswith(".sig-premium") and not self.is_premium:
            return JSONResponse(
                {"error": "Please upgrade to use this domain"}, status_code=403
            )

        email = f"{body['alias_prefix']}{suffix}"
        if any(a["email"] == email for a in self.aliases.values()):
            return JSONResponse({"error": "Alias already exists"}, status_code=409)
        if email in self.deleted_alias_ids:
            return JSONResponse({"error": "Alias already used"}, status_code=409)

        alias = self.add_alias(
            email=email, note=body.get("note"), name=body.get("name")
        )
        return JSONResponse(alias, status_code=201)

    async def _create_random(self, request: Request) -> JSONResponse:
        body = {}
        if await request.body():
            body = await request.json()
        if self._quota_exceeded():
            return JSONResponse(
                {"error": "You have reached the limitation of a free account"},
                status_code=400,
            )
        word = next(self._random_words)
        alias = self.add_alias(email=f"random{word}.mock@aleeas.com", note=body.get("note"))
        return JSONResponse(alias, status_code=201)

    async def _list_aliases(self, request: Request) -> JSONResponse:
        items = list(self.aliases.values())

        if request.query_params.get("pinned") is not None:
            items = [a for a in items if a["pinned"]]
        elif request.query_params.get("disabled") is not None:
            items = [a for a in items if not a["enabled"]]
        elif request.query_params.get("enabled") is not None:
            items = [a for a in items if a["enabled"]]

        if request.method == "POST" and await request.body():
            query = (await request.json()).get("query")
            if query:
                needle = query.lower()
                items = [
                    a
                    for a in items
                    if needle in a["email"].lower()
                    or needle in (a.get("note") or "").lower()
                ]

        return JSONResponse({"aliases": self._page(items, request)})

    async def _get_alias(self, request: Request) -> JSONResponse:
        alias = self.aliases.get(int(request.path_params["alias_id"]))
        if alias is None:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        return JSONResponse(alias)

    async def _toggle_alias(self, request: Request) -> JSONResponse:
        alias = self.aliases.get(int(request.path_params["alias_id"]))
        if alias is None:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        alias["enabled"] = not alias["enabled"]
        return JSONResponse({"enabled": alias["enabled"]})

    async def _update_alias(self, request: Request) -> JSONResponse:
        alias = self.aliases.get(int(request.path_params["alias_id"]))
        if alias is None:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        body = await request.json()
        for field in ("note", "name", "disable_pgp", "pinned"):
            if body.get(field) is not None:
                alias[field] = body[field]
        if body.get("mailbox_ids"):
            by_id = {m["id"]: m for m in self.mailboxes}
            missing = [i for i in body["mailbox_ids"] if i not in by_id]
            if missing:
                return JSONResponse(
                    {"error": f"Mailbox {missing[0]} does not exist"}, status_code=400
                )
            alias["mailboxes"] = [
                {"id": by_id[i]["id"], "email": by_id[i]["email"]}
                for i in body["mailbox_ids"]
            ]
        # The real API answers PATCH with a bare 200 and no body.
        return JSONResponse(None, status_code=200)

    async def _activities(self, request: Request) -> JSONResponse:
        alias_id = int(request.path_params["alias_id"])
        if alias_id not in self.aliases:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        return JSONResponse(
            {"activities": self._page(self.activities.get(alias_id, []), request)}
        )

    async def _list_contacts(self, request: Request) -> JSONResponse:
        alias_id = int(request.path_params["alias_id"])
        if alias_id not in self.aliases:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        return JSONResponse(
            {"contacts": self._page(self.contacts.get(alias_id, []), request)}
        )

    async def _create_contact(self, request: Request) -> JSONResponse:
        alias_id = int(request.path_params["alias_id"])
        if alias_id not in self.aliases:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        if not self.can_create_reverse_alias:
            return JSONResponse(
                {"error": "Please upgrade to create a reverse-alias"}, status_code=403
            )

        address = (await request.json()).get("contact")
        if not address:
            return JSONResponse({"error": "contact is required"}, status_code=400)

        existing = next(
            (c for c in self.contacts[alias_id] if c["contact"] == address), None
        )
        if existing is not None:
            return JSONResponse({**existing, "existed": True}, status_code=200)

        contact = {
            "id": next(self._contact_ids),
            "contact": address,
            "creation_date": "2026-08-02 12:00:00+00:00",
            "creation_timestamp": 1785686400,
            "last_email_sent_date": None,
            "last_email_sent_timestamp": None,
            "reverse_alias": f"{address} <ra+mock@aleeas.com>",
            "reverse_alias_address": "ra+mock@aleeas.com",
            "block_forward": False,
            "existed": False,
        }
        self.contacts[alias_id].append(contact)
        return JSONResponse(contact, status_code=201)

    async def _delete_alias(self, request: Request) -> JSONResponse:
        # Present so tests can prove the server never reaches it.
        alias_id = int(request.path_params["alias_id"])
        if alias_id not in self.aliases:
            return JSONResponse({"error": "Alias not found"}, status_code=404)
        del self.aliases[alias_id]
        self.deleted_alias_ids.append(alias_id)
        return JSONResponse({"deleted": True})

    async def _list_mailboxes(self, request: Request) -> JSONResponse:
        return JSONResponse({"mailboxes": self.mailboxes})

    async def _user_info(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "Test Account",
                "email": "owner@example.com",
                "is_premium": self.is_premium,
                "in_trial": False,
                "trial_end_timestamp": None,
                "max_alias_free_plan": self.max_alias_free_plan,
                "can_create_reverse_alias": self.can_create_reverse_alias,
                "profile_picture_url": None,
            }
        )

    # --------------------------------------------------------------------- app

    def app(self) -> Starlette:
        routes = [
            Route("/api/v5/alias/options", self._options, methods=["GET"]),
            Route("/api/v3/alias/custom/new", self._create_custom, methods=["POST"]),
            Route("/api/alias/random/new", self._create_random, methods=["POST"]),
            Route("/api/v2/aliases", self._list_aliases, methods=["GET", "POST"]),
            Route("/api/v2/mailboxes", self._list_mailboxes, methods=["GET"]),
            Route("/api/user_info", self._user_info, methods=["GET"]),
            Route("/api/aliases/{alias_id:int}", self._get_alias, methods=["GET"]),
            Route("/api/aliases/{alias_id:int}", self._update_alias, methods=["PATCH"]),
            Route("/api/aliases/{alias_id:int}", self._delete_alias, methods=["DELETE"]),
            Route(
                "/api/aliases/{alias_id:int}/toggle", self._toggle_alias, methods=["POST"]
            ),
            Route(
                "/api/aliases/{alias_id:int}/activities", self._activities, methods=["GET"]
            ),
            Route(
                "/api/aliases/{alias_id:int}/contacts",
                self._list_contacts,
                methods=["GET"],
            ),
            Route(
                "/api/aliases/{alias_id:int}/contacts",
                self._create_contact,
                methods=["POST"],
            ),
        ]

        async def record_and_authenticate(request: Request, call_next: Any) -> Any:
            self.request_log.append((request.method, request.url.path))
            if not self._auth_ok(request):
                return JSONResponse({"error": "Wrong api key"}, status_code=401)
            return await call_next(request)

        return Starlette(
            routes=routes,
            middleware=[
                Middleware(BaseHTTPMiddleware, dispatch=record_and_authenticate)
            ],
        )
