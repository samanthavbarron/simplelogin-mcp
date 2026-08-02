"""Configuration parsing, with emphasis on failing loudly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from simplelogin_mcp.config import Settings
from simplelogin_mcp.permissions import PermissionLevel


def build(**overrides: object) -> Settings:
    return Settings(SIMPLELOGIN_API_KEY="test-key", **overrides)


class TestPermissionLevel:
    def test_defaults_to_the_least_privileged_level(self) -> None:
        """An operator who configures nothing must not get write access."""
        assert build().permission_level is PermissionLevel.READ

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("read", PermissionLevel.READ),
            ("CREATE", PermissionLevel.CREATE),
            (" update ", PermissionLevel.UPDATE),
            ("delete", PermissionLevel.DELETE),
            ("0", PermissionLevel.READ),
            (3, PermissionLevel.DELETE),
        ],
    )
    def test_accepts_names_and_ordinals(
        self, raw: object, expected: PermissionLevel
    ) -> None:
        assert build(SIMPLELOGIN_PERMISSION_LEVEL=raw).permission_level is expected

    def test_rejects_a_typo_rather_than_defaulting(self) -> None:
        """Silently falling back would hand the operator a level they did not ask for."""
        with pytest.raises(ValidationError) as excinfo:
            build(SIMPLELOGIN_PERMISSION_LEVEL="readonly")
        message = str(excinfo.value)
        assert "readonly" in message
        assert "read, create, update, delete" in message

    def test_rejects_out_of_range_ordinals(self) -> None:
        with pytest.raises(ValidationError):
            build(SIMPLELOGIN_PERMISSION_LEVEL=99)


class TestRequiredFields:
    def test_api_key_is_mandatory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SIMPLELOGIN_API_KEY", raising=False)
        with pytest.raises(ValidationError):
            Settings()  # type: ignore[call-arg]

    def test_secrets_are_not_exposed_by_repr(self) -> None:
        """Guards against leaking credentials into logs or tracebacks."""
        settings = build(MCP_AUTH_TOKEN="super-secret-token")
        rendered = repr(settings) + str(settings)
        assert "test-key" not in rendered
        assert "super-secret-token" not in rendered
        assert settings.api_key.get_secret_value() == "test-key"


class TestNormalisation:
    def test_base_url_trailing_slash_is_stripped(self) -> None:
        settings = build(SIMPLELOGIN_API_BASE_URL="https://example.com/")
        assert settings.api_base_url == "https://example.com"

    def test_mcp_path_gets_a_leading_slash(self) -> None:
        assert build(MCP_PATH="mcp").path == "/mcp"

    def test_auth_token_defaults_to_absent(self) -> None:
        assert build().auth_token is None

    def test_max_auto_pages_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            build(SIMPLELOGIN_MAX_AUTO_PAGES=0)


class TestEnvironment:
    def test_reads_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIMPLELOGIN_API_KEY", "from-env")
        monkeypatch.setenv("SIMPLELOGIN_PERMISSION_LEVEL", "update")
        monkeypatch.setenv("MCP_PORT", "9999")
        settings = Settings()  # type: ignore[call-arg]
        assert settings.api_key.get_secret_value() == "from-env"
        assert settings.permission_level is PermissionLevel.UPDATE
        assert settings.port == 9999
