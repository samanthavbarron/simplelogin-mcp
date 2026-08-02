"""MCP tool definitions, one per SimpleLogin endpoint.

Every tool carries a ``perm:<level>`` tag naming the permission level it needs.
That tag is the single source of truth for gating: the middleware in
``server.py`` reads it both when listing tools and when calling them.

Note that classification follows the *operation*, not the HTTP verb --
``search_aliases`` issues a POST but is a read, and is tagged accordingly.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import SimpleLoginClient
from .errors import SimpleLoginError
from .permissions import PermissionLevel

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

#: Sentinel meaning "no explicit page -- auto-paginate from the first".
AUTO_PAGINATE = -1

#: ``page_id`` is deliberately a plain ``int`` with a sentinel rather than the
#: more natural ``int | None``.
#:
#: An optional int renders as ``anyOf: [{"type": "integer"}, {"type": "null"}]``.
#: LiteLLM's MCP gateway validates arguments against the published schema before
#: forwarding them, and the values arrive there as strings -- a string satisfies
#: neither branch, so every paginated call failed with "'0' is not valid under
#: any of the given schemas" (observed in production 2026-08-02). A bare
#: ``{"type": "integer"}`` is coerced instead, and works.
#:
#: This cannot be fixed server-side: the rejection happens upstream, against the
#: schema we publish, before the request ever reaches us. Optional *string*
#: parameters are unaffected, because a string matches their string branch.
PageId = Annotated[
    int,
    Field(
        ge=AUTO_PAGINATE,
        description=(
            "0-based page, 20 items per page. Omit (or pass -1) to auto-paginate "
            "from the first page up to the server's page cap."
        ),
    ),
]


def _explicit_page(page_id: int) -> int | None:
    """Map the sentinel back onto the client's optional ``page_id``."""
    return None if page_id <= AUTO_PAGINATE else page_id


def _surface_api_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn SimpleLogin failures into ToolError with the API's own wording.

    SimpleLogin's messages ("you have reached the maximum number of aliases",
    "please upgrade to create a reverse-alias") are written for humans and are
    more useful to a model than anything we would substitute.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except SimpleLoginError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def register_tools(
    mcp: FastMCP, client: SimpleLoginClient, *, max_auto_pages: int = 10
) -> None:
    """Register every tool on ``mcp``, regardless of permission level.

    All tools are registered unconditionally so the middleware can distinguish
    "this tool exists but you may not use it" from "no such tool". Registering
    only the permitted subset would collapse both into an unknown-tool error and
    leave nothing for the second enforcement layer to do.
    """

    # ------------------------------------------------------------------- reads

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description=(
            "Get the options for creating a new alias: which domain suffixes are "
            "available, whether the account may create more aliases, and a suggested "
            "prefix. Call this before create_custom_alias to obtain a signed_suffix."
        ),
    )
    @_surface_api_errors
    async def get_alias_options(
        hostname: Annotated[
            str | None,
            Field(
                description=(
                    "Hostname of the site the alias is for, e.g. 'example.com'. "
                    "Drives prefix_suggestion and may surface an alias already used "
                    "for this site."
                )
            ),
        ] = None,
    ) -> Any:
        return await client.get_alias_options(hostname)

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description=(
            "List the account's aliases. Omit page_id to auto-paginate; supply it "
            "(0-based, 20 per page) to fetch one page at a time."
        ),
    )
    @_surface_api_errors
    async def list_aliases(
        page_id: PageId = AUTO_PAGINATE,
        filter_by: Annotated[
            Literal["pinned", "disabled", "enabled"] | None,
            Field(description="Return only aliases in this state."),
        ] = None,
    ) -> Any:
        return await client.list_aliases(
            page_id=_explicit_page(page_id),
            max_pages=max_auto_pages,
            filter_by=filter_by,
        )

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description="Search the account's aliases by free-text query.",
    )
    @_surface_api_errors
    async def search_aliases(
        query: Annotated[str, Field(description="Free-text search over aliases.")],
        page_id: PageId = AUTO_PAGINATE,
    ) -> Any:
        return await client.list_aliases(
            query=query, page_id=_explicit_page(page_id)
        )

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description="Get full information about one alias by its numeric id.",
    )
    @_surface_api_errors
    async def get_alias(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
    ) -> Any:
        return await client.get_alias(alias_id)

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description=(
            "Get the activity log for an alias: emails forwarded, replied and "
            "blocked, with timestamps and counterparties."
        ),
    )
    @_surface_api_errors
    async def get_alias_activities(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
        page_id: PageId = AUTO_PAGINATE,
    ) -> Any:
        return await client.get_alias_activities(
            alias_id, page_id=_explicit_page(page_id), max_pages=max_auto_pages
        )

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description="List the contacts (reverse-aliases) configured for an alias.",
    )
    @_surface_api_errors
    async def list_alias_contacts(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
        page_id: PageId = AUTO_PAGINATE,
    ) -> Any:
        return await client.list_alias_contacts(
            alias_id, page_id=_explicit_page(page_id), max_pages=max_auto_pages
        )

    @mcp.tool(
        tags={PermissionLevel.READ.tag},
        annotations=READ_ONLY,
        description=(
            "List the account's mailboxes. Use this to obtain the mailbox_ids "
            "required by create_custom_alias and update_alias."
        ),
    )
    @_surface_api_errors
    async def list_mailboxes() -> Any:
        return await client.list_mailboxes()

    # ----------------------------------------------------------------- creates

    @mcp.tool(
        tags={PermissionLevel.CREATE.tag},
        annotations=ADDITIVE,
        description=(
            "Create a new alias with a chosen prefix. Requires a signed_suffix from "
            "get_alias_options and mailbox_ids from list_mailboxes. Note that the "
            "resulting address is permanent -- it cannot be renamed later."
        ),
    )
    @_surface_api_errors
    async def create_custom_alias(
        alias_prefix: Annotated[
            str,
            Field(
                description=(
                    "The chosen first part of the address. Letters, digits, dots, "
                    "hyphens and underscores."
                )
            ),
        ],
        signed_suffix: Annotated[
            str,
            Field(
                description=(
                    "A signed_suffix value taken verbatim from get_alias_options. "
                    "It is cryptographically signed and cannot be constructed by hand."
                )
            ),
        ],
        mailbox_ids: Annotated[
            list[int],
            Field(
                min_length=1,
                description="Mailbox ids that receive mail for this alias.",
            ),
        ],
        note: Annotated[str | None, Field(description="Free-text note.")] = None,
        name: Annotated[
            str | None, Field(description="Display name used when sending.")
        ] = None,
        hostname: Annotated[
            str | None, Field(description="Site this alias is for, e.g. 'example.com'.")
        ] = None,
    ) -> Any:
        return await client.create_custom_alias(
            alias_prefix=alias_prefix,
            signed_suffix=signed_suffix,
            mailbox_ids=mailbox_ids,
            note=note,
            name=name,
            hostname=hostname,
        )

    @mcp.tool(
        tags={PermissionLevel.CREATE.tag},
        annotations=ADDITIVE,
        description=(
            "Create a randomly generated alias. The generation style and domain "
            "follow the account's own settings."
        ),
    )
    @_surface_api_errors
    async def create_random_alias(
        note: Annotated[str | None, Field(description="Free-text note.")] = None,
        hostname: Annotated[
            str | None, Field(description="Site this alias is for, e.g. 'example.com'.")
        ] = None,
    ) -> Any:
        return await client.create_random_alias(note=note, hostname=hostname)

    @mcp.tool(
        tags={PermissionLevel.CREATE.tag},
        annotations=ADDITIVE,
        description=(
            "Create a contact (reverse-alias) for an alias, letting the account send "
            "mail to that address from the alias. Requires a paid plan; free accounts "
            "receive an upgrade-required error."
        ),
    )
    @_surface_api_errors
    async def create_alias_contact(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
        contact: Annotated[
            str,
            Field(
                description=(
                    "Destination address, optionally with a display name, e.g. "
                    "'First Last <first@example.com>'."
                )
            ),
        ],
    ) -> Any:
        return await client.create_alias_contact(alias_id, contact)

    # ----------------------------------------------------------------- updates

    @mcp.tool(
        tags={PermissionLevel.UPDATE.tag},
        annotations=ADDITIVE,
        description=(
            "Update an alias's note, display name, owning mailboxes, PGP setting or "
            "pinned state. At least one field must be supplied."
        ),
    )
    @_surface_api_errors
    async def update_alias(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
        note: Annotated[str | None, Field(description="Replacement note.")] = None,
        name: Annotated[
            str | None, Field(description="Replacement display name.")
        ] = None,
        mailbox_ids: Annotated[
            list[int] | None,
            Field(description="Replacement set of owning mailbox ids."),
        ] = None,
        disable_pgp: Annotated[
            bool | None, Field(description="Disable PGP even if mailboxes support it.")
        ] = None,
        pinned: Annotated[bool | None, Field(description="Pin or unpin.")] = None,
    ) -> Any:
        return await client.update_alias(
            alias_id,
            note=note,
            name=name,
            mailbox_ids=mailbox_ids,
            disable_pgp=disable_pgp,
            pinned=pinned,
        )

    @mcp.tool(
        tags={PermissionLevel.UPDATE.tag},
        annotations=ADDITIVE,
        description=(
            "Enable or disable an alias, toggling its current state. A disabled alias "
            "silently stops forwarding mail but is preserved and can be re-enabled. "
            "This is the non-destructive way to retire an alias."
        ),
    )
    @_surface_api_errors
    async def toggle_alias(
        alias_id: Annotated[int, Field(description="Numeric alias id.")],
    ) -> Any:
        return await client.toggle_alias(alias_id)
