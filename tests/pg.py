"""A throwaway PostgreSQL for the test session.

The application is PostgreSQL-only, so the suite needs a real server. This starts
one in Docker, waits for it, and removes it afterwards — no local install, no
shared state, and the same image the compose file uses.

Set ``LAYA_STUDIO_TEST_DATABASE_URL`` to point the suite at a server you manage
instead; Docker is then not needed at all (CI usually does this).
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

IMAGE = os.environ.get("LAYA_STUDIO_TEST_PG_IMAGE", "postgres:16-alpine")
STARTUP_TIMEOUT = 60.0


class PostgresUnavailable(RuntimeError):
    """No usable PostgreSQL, and no way to start one."""


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=120)


@dataclass
class PostgresServer:
    url: str
    container: str | None = None

    def stop(self) -> None:
        if self.container:
            _run(["docker", "rm", "-f", self.container], check=False)


def _wait_until_ready(container: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        probe = _run(
            ["docker", "exec", container, "pg_isready", "-U", "laya", "-d", "laya_test"],
            check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    raise PostgresUnavailable(
        f"The test Postgres container did not become ready within {STARTUP_TIMEOUT:.0f}s."
    )


def start() -> PostgresServer:
    """Return a usable PostgreSQL, starting a container if necessary."""
    external = os.environ.get("LAYA_STUDIO_TEST_DATABASE_URL")
    if external:
        return PostgresServer(url=external)

    if not _docker_available():
        raise PostgresUnavailable(
            "No PostgreSQL available for the test suite.\n\n"
            "Either start Docker (the suite runs a throwaway postgres:16 container), or\n"
            "point the suite at your own server:\n\n"
            "    export LAYA_STUDIO_TEST_DATABASE_URL="
            "'postgresql://user:pass@localhost:5432/laya_test'\n"
        )

    container = f"laya-studio-test-pg-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    try:
        _run(
            [
                "docker", "run", "-d", "--rm",
                "--name", container,
                "-e", "POSTGRES_USER=laya",
                "-e", "POSTGRES_PASSWORD=laya",
                "-e", "POSTGRES_DB=laya_test",
                "-p", f"{port}:5432",
                IMAGE,
            ]
        )
        _wait_until_ready(container)
    except Exception as exc:  # noqa: BLE001 - clean up then report
        _run(["docker", "rm", "-f", container], check=False)
        raise PostgresUnavailable(f"Could not start a test PostgreSQL: {exc}") from exc

    return PostgresServer(
        url=f"postgresql://laya:laya@127.0.0.1:{port}/laya_test",
        container=container,
    )
