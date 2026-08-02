"""Unit tests for the permission primitives."""

from __future__ import annotations

import pytest

from simplelogin_mcp.permissions import PermissionLevel, required_level


class TestOrdering:
    def test_levels_are_cumulative(self) -> None:
        assert PermissionLevel.READ < PermissionLevel.CREATE
        assert PermissionLevel.CREATE < PermissionLevel.UPDATE
        assert PermissionLevel.UPDATE < PermissionLevel.DELETE

    @pytest.mark.parametrize(
        ("configured", "required", "allowed"),
        [
            (PermissionLevel.READ, PermissionLevel.READ, True),
            (PermissionLevel.READ, PermissionLevel.CREATE, False),
            (PermissionLevel.READ, PermissionLevel.DELETE, False),
            (PermissionLevel.CREATE, PermissionLevel.READ, True),
            (PermissionLevel.CREATE, PermissionLevel.CREATE, True),
            (PermissionLevel.CREATE, PermissionLevel.UPDATE, False),
            (PermissionLevel.UPDATE, PermissionLevel.CREATE, True),
            (PermissionLevel.UPDATE, PermissionLevel.DELETE, False),
            (PermissionLevel.DELETE, PermissionLevel.DELETE, True),
        ],
    )
    def test_permits(
        self, configured: PermissionLevel, required: PermissionLevel, allowed: bool
    ) -> None:
        assert configured.permits(required) is allowed


class TestParsing:
    @pytest.mark.parametrize("raw", ["read", "READ", "Read", " read "])
    def test_accepts_case_and_whitespace_variants(self, raw: str) -> None:
        assert PermissionLevel(raw) is PermissionLevel.READ

    def test_rejects_unknown_names(self) -> None:
        with pytest.raises(ValueError):
            PermissionLevel("readonly")


class TestTags:
    def test_tag_round_trips(self) -> None:
        for level in PermissionLevel:
            assert required_level({level.tag}) is level

    def test_absent_tag_yields_none(self) -> None:
        """None means "refuse"; the middleware must never read it as "allow"."""
        assert required_level(set()) is None
        assert required_level(None) is None
        assert required_level({"unrelated", "tags"}) is None

    def test_unparseable_permission_tag_yields_none(self) -> None:
        assert required_level({"perm:superuser"}) is None

    def test_multiple_tags_resolve_to_the_strictest(self) -> None:
        assert (
            required_level({PermissionLevel.READ.tag, PermissionLevel.UPDATE.tag})
            is PermissionLevel.UPDATE
        )
