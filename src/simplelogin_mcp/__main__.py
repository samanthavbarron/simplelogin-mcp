"""Entry point: validate configuration, then serve."""

from __future__ import annotations

import logging
import sys

import uvicorn
from pydantic import ValidationError

from .config import Settings
from .server import build_http_app

logger = logging.getLogger("simplelogin_mcp")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Fail fast and legibly: a container that boots with the wrong permission
        # level is worse than one that refuses to boot.
        print("Invalid configuration:", file=sys.stderr)
        for error in exc.errors():
            location = " / ".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        return 2

    logger.info(
        "starting on %s:%s%s at permission level '%s' (bearer auth %s)",
        settings.host,
        settings.port,
        settings.path,
        settings.permission_level.name.lower(),
        "enabled" if settings.auth_token else "disabled",
    )
    if settings.auth_token is None:
        logger.warning(
            "MCP_AUTH_TOKEN is not set: the endpoint is unauthenticated. "
            "Restrict access at the network layer."
        )

    uvicorn.run(
        build_http_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
