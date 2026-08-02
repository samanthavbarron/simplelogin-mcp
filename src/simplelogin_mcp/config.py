"""Runtime configuration, sourced from the environment."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .permissions import PermissionLevel


class Settings(BaseSettings):
    """Deployment configuration.

    One SimpleLogin account and one permission level per deployment. Running at
    several permission levels means running several containers.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    api_key: SecretStr = Field(validation_alias="SIMPLELOGIN_API_KEY")
    api_base_url: str = Field(
        default="https://app.simplelogin.io",
        validation_alias="SIMPLELOGIN_API_BASE_URL",
    )
    permission_level: PermissionLevel = Field(
        default=PermissionLevel.READ,
        validation_alias="SIMPLELOGIN_PERMISSION_LEVEL",
    )
    request_timeout: float = Field(
        default=30.0, validation_alias="SIMPLELOGIN_REQUEST_TIMEOUT"
    )
    max_auto_pages: int = Field(
        default=10,
        ge=1,
        validation_alias="SIMPLELOGIN_MAX_AUTO_PAGES",
        description="Page cap applied when a list tool is called without page_id.",
    )

    auth_token: SecretStr | None = Field(
        default=None,
        validation_alias="MCP_AUTH_TOKEN",
        description="If set, clients must present it as a bearer token.",
    )
    host: str = Field(default="0.0.0.0", validation_alias="MCP_HOST")
    port: int = Field(default=8000, validation_alias="MCP_PORT")
    path: str = Field(default="/mcp", validation_alias="MCP_PATH")

    @field_validator("permission_level", mode="before")
    @classmethod
    def _parse_level(cls, value: Any) -> Any:
        """Accept ``read``/``READ``/``0`` and reject anything else loudly.

        Defaulting an unrecognised level would be dangerous: a typo such as
        ``SIMPLELOGIN_PERMISSION_LEVEL=readonly`` must not silently land on a
        level the operator did not intend.
        """
        if isinstance(value, str):
            raw = value.strip()
            if raw.isdigit():
                value = int(raw)
            else:
                try:
                    return PermissionLevel[raw.upper()]
                except KeyError:
                    valid = ", ".join(lv.name.lower() for lv in PermissionLevel)
                    raise ValueError(
                        f"unknown permission level {value!r}; expected one of: {valid}"
                    ) from None
        return value

    @field_validator("api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("path")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"
