"""Permission levels and the tag scheme used to gate tools.

Levels are cumulative: a deployment configured at ``UPDATE`` may perform every
operation tagged ``READ``, ``CREATE`` or ``UPDATE``, but nothing tagged
``DELETE``.

``DELETE`` currently grants no tools. Alias deletion is deliberately not exposed
(see PROJECT_GOALS.md), and it was the only destructive operation in scope. The
level is retained so that configuration and the hierarchy stay forward
compatible, and so the invariant "DELETE adds nothing beyond UPDATE" is an
assertion the test suite can make rather than an accident.
"""

from __future__ import annotations

from enum import IntEnum

TAG_PREFIX = "perm:"


class PermissionLevel(IntEnum):
    """Cumulative capability level for a deployment."""

    READ = 0
    CREATE = 1
    UPDATE = 2
    DELETE = 3

    @classmethod
    def _missing_(cls, value: object) -> PermissionLevel | None:
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                return None
        return None

    @property
    def tag(self) -> str:
        return f"{TAG_PREFIX}{self.name.lower()}"

    def permits(self, required: PermissionLevel) -> bool:
        return self >= required


def required_level(tags: object) -> PermissionLevel | None:
    """Extract the level a tool requires from its tag set.

    Returns ``None`` when no permission tag is present. Callers must treat that
    as "refuse", not "allow" -- an untagged tool is a registration bug, and
    failing open would silently hand out capabilities.
    """
    if not tags:
        return None
    found: list[PermissionLevel] = []
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(TAG_PREFIX):
            level = PermissionLevel._missing_(tag[len(TAG_PREFIX) :])
            if level is not None:
                found.append(level)
    if not found:
        return None
    # A tool carrying several permission tags is ambiguous; demand the strictest.
    return max(found)
