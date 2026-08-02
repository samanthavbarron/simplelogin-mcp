"""Safety machinery for tests that hit the real SimpleLogin account.

The account is shared, mutable, small (free-tier quota) and contains data this
suite did not create. Three rules follow from that:

* Everything this suite creates is stamped with :data:`MARKER`, and only stamped
  aliases are ever deleted. The account's pre-existing aliases -- SimpleLogin
  seeds every new account with a newsletter alias -- must survive untouched.
* Deletion happens here, through a direct API client, never through the server
  under test. The server has no delete path by design, and teardown must not
  depend on the code it is testing.
* Custom aliases permanently reserve their address, so the suite creates as few
  as possible and prefers random aliases.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

MARKER = "simplelogin-mcp-e2e"
DEFAULT_BASE_URL = "https://app.simplelogin.io"

#: Marks the long-lived alias shared across runs. SimpleLogin rate limits alias
#: *creation* far more aggressively than anything else -- an exhausted window was
#: measured still refusing after six minutes of idle -- while reads, PATCH and
#: toggle stay unaffected. So the suite keeps one durable alias and reuses it,
#: rather than minting a fresh one per test and throttling itself out.
PERSISTENT = "persistent-fixture"

#: Leave headroom below the free-tier cap so a run never fills the account.
QUOTA_HEADROOM = 3


class RateLimited(Exception):
    """SimpleLogin is throttling alias creation.

    Distinguished from a test failure: it reflects account state, not a defect
    in the server under test.
    """


def run_id() -> str:
    """Identify this run, preferring CI's run id so leftovers are traceable."""
    ci_run = os.environ.get("GITHUB_RUN_ID")
    suffix = uuid.uuid4().hex[:8]
    return f"{ci_run}-{suffix}" if ci_run else suffix


def note_for(run: str, detail: str = "") -> str:
    stamp = f"{MARKER}/{run}"
    return f"{stamp} {detail}".strip()


def is_ours(alias: dict[str, Any]) -> bool:
    return MARKER in (alias.get("note") or "")


def is_persistent(alias: dict[str, Any]) -> bool:
    """The shared fixture alias, which survives teardown so runs need not create."""
    return is_ours(alias) and PERSISTENT in (alias.get("note") or "")


def is_ephemeral(alias: dict[str, Any]) -> bool:
    return is_ours(alias) and not is_persistent(alias)


@dataclass
class LiveAccount:
    """Direct API access for setup, verification and teardown.

    Deliberately separate from ``SimpleLoginClient``: this one can delete, and
    it must keep working even if the client under test is broken.
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    _http: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers={"Authentication": self.api_key},
            timeout=30.0,
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ reads

    def user_info(self) -> dict[str, Any]:
        response = self._http.get("/api/user_info")
        response.raise_for_status()
        return response.json()

    def all_aliases(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 0
        while True:
            response = self._http.get("/api/v2/aliases", params={"page_id": page})
            response.raise_for_status()
            batch = response.json().get("aliases", [])
            collected.extend(batch)
            if len(batch) < 20:
                return collected
            page += 1

    def get_alias(self, alias_id: int) -> dict[str, Any] | None:
        response = self._http.get(f"/api/aliases/{alias_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------ restoration

    def restore_alias(self, alias_id: int, original: dict[str, Any]) -> None:
        """Put a shared alias back the way it was found.

        Used after tests that mutate the persistent fixture, so each test starts
        from the same state regardless of what ran before it.
        """
        self._http.patch(
            f"/api/aliases/{alias_id}",
            json={
                "note": original.get("note") or "",
                "name": original.get("name"),
                "pinned": bool(original.get("pinned")),
            },
        )
        if self.get_alias(alias_id) is not None:
            self.set_enabled(alias_id, bool(original.get("enabled", True)))

    def set_enabled(self, alias_id: int, enabled: bool) -> None:
        """Force an alias into a given state; toggle is the only lever available."""
        current = self.get_alias(alias_id)
        if current is None or bool(current["enabled"]) == enabled:
            return
        self._http.post(f"/api/aliases/{alias_id}/toggle")

    # --------------------------------------------------------------- teardown

    def delete_alias(self, alias_id: int) -> bool:
        response = self._http.delete(f"/api/aliases/{alias_id}")
        # 404 means it is already gone, which satisfies the intent.
        return response.status_code in (200, 404)

    def sweep(self, *, only_run: str | None = None) -> list[int]:
        """Delete the ephemeral aliases this suite created.

        Two things are deliberately spared: anything without the marker, which
        belongs to the account owner, and the persistent fixture alias, whose
        whole purpose is to outlive the run.
        """
        removed: list[int] = []
        for alias in self.all_aliases():
            if not is_ephemeral(alias):
                continue
            if only_run is not None and f"{MARKER}/{only_run}" not in (
                alias.get("note") or ""
            ):
                continue
            if self.delete_alias(alias["id"]):
                removed.append(alias["id"])
        return removed

    def persistent_alias(self) -> dict[str, Any] | None:
        return next((a for a in self.all_aliases() if is_persistent(a)), None)

    def ensure_persistent_alias(self) -> dict[str, Any]:
        """Return the shared fixture alias, creating it only if truly absent.

        Creation is the throttled operation, so this is the one place allowed to
        perform it, and only on the rare run where the alias has gone missing.
        """
        existing = self.persistent_alias()
        if existing is not None:
            return existing

        response = self._http.post(
            "/api/alias/random/new",
            json={"note": f"{MARKER}/{PERSISTENT} do not delete: shared test fixture"},
        )
        if response.status_code == 429:
            raise RateLimited(
                "the shared fixture alias is missing and SimpleLogin is currently "
                "rate limiting alias creation; retry once the window clears"
            )
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------- preflight

    def preflight(self, *, needed: int) -> None:
        """Sweep leftovers, then confirm there is room to work.

        Raises rather than skipping: an account still full after a sweep means
        the cleanup path is broken, which is precisely what should be noticed.
        """
        orphans = self.sweep()
        info = self.user_info()

        # Treat the account as free tier regardless of an active trial, so the
        # suite keeps working the day the trial lapses.
        cap = int(info.get("max_alias_free_plan") or 10)
        current = len(self.all_aliases())

        if current + needed + QUOTA_HEADROOM > cap:
            surviving = [
                f"{a['id']}:{a['email']}" for a in self.all_aliases() if not is_ours(a)
            ]
            raise RuntimeError(
                f"insufficient alias quota: {current} of {cap} used, need {needed} "
                f"plus {QUOTA_HEADROOM} headroom. Swept {len(orphans)} leftover "
                f"alias(es) from previous runs. Remaining aliases are not owned by "
                f"this suite and were left alone: {surviving}"
            )

    def wait_for_alias(self, alias_id: int, timeout: float = 10.0) -> dict[str, Any]:
        """Poll until an alias is visible, tolerating brief propagation lag."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alias = self.get_alias(alias_id)
            if alias is not None:
                return alias
            time.sleep(0.5)
        raise AssertionError(f"alias {alias_id} never became visible")


#: SimpleLogin throttles alias creation. Back off and retry rather than pacing
#: every test with a fixed sleep, which would slow the suite even when idle.
RATE_LIMIT_BACKOFF = (10.0, 20.0, 30.0, 45.0)


async def call_with_retry(
    client: Any,
    tool: str,
    args: dict[str, Any],
    *,
    delays: tuple[float, ...] = RATE_LIMIT_BACKOFF,
) -> Any:
    """Call an MCP tool, retrying only when SimpleLogin reports rate limiting.

    Any other error propagates immediately -- retrying a genuine failure would
    just make the suite slow and the diagnosis harder.
    """
    import asyncio

    from fastmcp.exceptions import ToolError

    for delay in delays:
        try:
            return (await client.call_tool(tool, args)).data
        except ToolError as exc:
            if "rate limit" not in str(exc).lower():
                raise
            await asyncio.sleep(delay)

    try:
        return (await client.call_tool(tool, args)).data
    except ToolError as exc:
        if "rate limit" in str(exc).lower():
            # Surface as a distinct type so callers can skip rather than fail:
            # an exhausted creation window says nothing about the server.
            raise RateLimited(str(exc)) from exc
        raise


def free_signed_suffix(options: dict[str, Any]) -> str:
    """Pick a suffix usable without a paid plan.

    Taking the first offered suffix would pass during the trial and start
    failing the moment it expires.
    """
    for entry in options.get("suffixes", []):
        if not entry.get("is_premium"):
            return entry["signed_suffix"]
    raise AssertionError(
        "SimpleLogin offered no free-tier suffix; the account may have changed plan"
    )
