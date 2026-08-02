"""Fixtures for end-to-end tests that run the built container image.

The image under test is whatever ``SLMCP_IMAGE`` names, so CI can point these at
the artefact the build job actually produced rather than at a rebuild. Absent
that variable the image is built locally.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from tests.fake_simplelogin import FakeSimpleLogin

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = "simplelogin-mcp:test"
STARTUP_TIMEOUT = 30.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def container_engine() -> str | None:
    """Prefer whatever the operator asked for, else whichever engine works."""
    requested = os.environ.get("CONTAINER_ENGINE")
    if requested:
        return shutil.which(requested)

    for candidate in ("docker", "podman"):
        path = shutil.which(candidate)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "info"], capture_output=True, text=True, timeout=60
        )
        if probe.returncode == 0:
            return path
    return None


@pytest.fixture(scope="session")
def engine() -> str:
    found = container_engine()
    if found is None:
        pytest.skip("no working container engine (docker or podman) available")
    return found


@pytest.fixture(scope="session")
def image(engine: str) -> str:
    """The image under test, built locally only if one was not supplied."""
    supplied = os.environ.get("SLMCP_IMAGE")
    if supplied:
        return supplied

    subprocess.run(
        [engine, "build", "-t", DEFAULT_IMAGE, "."],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return DEFAULT_IMAGE


@pytest.fixture
def fake_upstream() -> Iterator[tuple[FakeSimpleLogin, int]]:
    """Run a fake SimpleLogin on the host for the container to talk to."""
    fake = FakeSimpleLogin()
    port = free_port()
    config = uvicorn.Config(
        fake.app(), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("fake SimpleLogin did not start")

    try:
        yield fake, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def run_container(
    engine: str, image: str
) -> Iterator[Callable[..., tuple[str, int]]]:
    """Start the image and wait until its health endpoint answers.

    Uses host networking so the container can reach the fake upstream on
    127.0.0.1 without per-engine gateway hostname differences.
    """
    started: list[str] = []

    def _run(**env: str) -> tuple[str, int]:
        port = free_port()
        args = [engine, "run", "-d", "--network=host", "-e", f"MCP_PORT={port}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args.append(image)

        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            pytest.fail(f"could not start container: {result.stderr}")
        container_id = result.stdout.strip()
        started.append(container_id)

        wait_for_health(engine, container_id, port)
        return container_id, port

    try:
        yield _run
    finally:
        for container_id in started:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run(
                    [engine, "rm", "-f", container_id],
                    capture_output=True,
                    timeout=60,
                )


def wait_for_health(engine: str, container_id: str, port: int) -> None:
    import httpx

    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        exited = subprocess.run(
            [engine, "inspect", "-f", "{{.State.Running}}", container_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if exited.stdout.strip() == "false":
            logs = subprocess.run(
                [engine, "logs", container_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
            pytest.fail(
                f"container exited during startup:\n{logs.stdout}\n{logs.stderr}"
            )

        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.25)

    logs = subprocess.run(
        [engine, "logs", container_id], capture_output=True, text=True, timeout=30
    )
    pytest.fail(
        f"container health check never passed ({last_error}):\n"
        f"{logs.stdout}\n{logs.stderr}"
    )


def container_env(fake_port: int, **overrides: str) -> dict[str, str]:
    """Standard environment pointing the container at the host-side fake."""
    return {
        "SIMPLELOGIN_API_KEY": "test-key",
        "SIMPLELOGIN_API_BASE_URL": f"http://127.0.0.1:{fake_port}",
        **overrides,
    }


def image_config(engine: str, image: str) -> dict:
    result = subprocess.run(
        [engine, "inspect", image],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)[0]
