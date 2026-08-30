"""The host-appliance driver for the super-E2E suite (#2561).

Starts the **real Docker host image** (``src/containers/host/`` — the
klangkd + supervisord + rootless-podman + caddy appliance) as a Docker
container with the shipped posture (``--cap-add SYS_ADMIN``, seccomp /
systempaths unconfined, ``/dev/fuse`` + ``/dev/net/tun`` when present —
the flags ``docs/deployment/docker.md`` documents for operators) and
exposes:

* the published browser port (``http://localhost:<mapped>``) for
  black-box HTTP / WebSocket clients, and
* a ``docker exec`` control channel for service-state checks and
  signals (SIGHUP to klangkd's PID, pgrep for the supervisord-managed
  children, rootless ``podman ps`` for nested workspace containers).

The suite never builds the image itself — CI builds it via
``build-host-image`` before running the suite, and locally the operator
does (``devenv shell -- build-host-image``). A missing image fails the
session fixture loudly (a skip would make CI silently green, the exact
gap #2561 exists to close).
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid

import httpx

# The browser port baked into the host image (klangkd.yaml listen 0.0.0.0,
# EXPOSE 8997). Only this port is published; the egress port (8995) stays
# internal to the appliance's container network, exactly as shipped.
_APPLIANCE_PORT = 8997

# How long the first boot may take. The entrypoint loads the embedded
# workspace + sidecar image tars into rootless podman (a minute+ on a
# cold CI runner), then klangkd runs its podman prewarm before /health
# answers. Generous on purpose — the failure mode to avoid is killing a
# healthy-but-slow first boot.
_BOOT_TIMEOUT_SECONDS = 600


def docker_path() -> str:
    """The docker binary, or raise with a clear message."""
    path = shutil.which("docker")
    if not path:
        raise RuntimeError(
            "super-e2e needs a docker daemon (docker not found on PATH). "
            "Run inside `devenv shell` on a machine with Docker."
        )
    return path


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def image_exists(image: str) -> bool:
    return _run(["docker", "image", "inspect", image]).returncode == 0


def _free_host_port() -> int:
    """A free TCP port on the host for ``-p <port>:8997``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Appliance:
    """One running instance of the klangk host container."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.name = f"klangk-super-e2e-{uuid.uuid4().hex[:8]}"
        self.port: int | None = None
        self.container_id: str | None = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Boot the appliance and block until ``/health`` answers 200."""
        if not image_exists(self.image):
            raise RuntimeError(
                f"Host image {self.image!r} not found. Build it first:\n"
                f"    devenv shell -- build-host-image\n"
                f"(or point KLANGK_SUPER_E2E_IMAGE at an existing image)"
            )
        docker_path()
        self.port = _free_host_port()
        cmd = ["docker", "run", "-d", "--name", self.name]
        # The shipped nested-podman posture (docs/deployment/docker.md):
        # SYS_ADMIN + relaxed seccomp/systempaths for userns + mounts,
        # /dev/fuse for fuse-overlayfs storage, /dev/net/tun for pasta
        # networking. Device flags are conditional — a runner may lack
        # them (the CDK pentest does the same).
        cmd += ["--cap-add", "SYS_ADMIN"]
        if os.path.exists("/dev/fuse"):
            cmd += ["--device", "/dev/fuse"]
        if os.path.exists("/dev/net/tun"):
            cmd += ["--device", "/dev/net/tun"]
        cmd += [
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "systempaths=unconfined",
            "--pull=never",
        ]
        for key, value in self._env().items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["-p", f"127.0.0.1:{self.port}:{_APPLIANCE_PORT}", self.image]
        result = _run(cmd, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker run failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        self.container_id = result.stdout.strip()
        self.wait_healthy()

    @staticmethod
    def _env() -> dict[str, str]:
        """The test config, injected as env vars on the container.

        Env vars override the baked ``klangkd.yaml`` (the image's own
        comment says so), so the appliance keeps its shipped ports/dirs
        while the test knobs (auth, short idle timeout, fast health
        poll, test mode) ride in from outside — the same knobs every
        e2e server launch sets. Password auth is required: the
        published port makes the bind non-loopback, and the no-auth
        safety gate (#1374) refuses `none` mode there.
        """
        return {
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DEFAULT_USER": "admin@example.com",
            "KLANGKD_DEFAULT_PASSWORD": "adminpass",
            # Long enough to pass any secret-strength gate; fixed so a
            # rerun against a kept data dir still validates tokens.
            "KLANGKD_JWT_SECRET": secrets.token_hex(32),
            "KLANGKD_TEST_MODE": "1",
            # Idle default mirrors the other e2e suites (300s). The
            # idle-stop test sets a per-workspace override instead, so
            # long holds (consent, exec) can't be reaped mid-test.
            "KLANGKD_IDLE_TIMEOUT_SECONDS": "300",
            # Fast health polling for the unhealthy-workspace test.
            "KLANGKD_HEALTH_CHECK_INTERVAL": "2",
            "KLANGKD_HEALTH_CHECK_STARTUP_GRACE": "0.1",
            # Bounded consent hold so a missed verdict can't stall a test.
            "KLANGKD_EGRESS_CONSENT_TIMEOUT": "12",
            "LOGFIRE_TOKEN": "",
        }

    # --- clients -------------------------------------------------------

    @property
    def url(self) -> str:
        assert self.port is not None, "appliance not started"
        return f"http://127.0.0.1:{self.port}"

    def wait_healthy(self, timeout: int = _BOOT_TIMEOUT_SECONDS) -> None:
        """Poll ``/health`` through the published port until 200."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        with httpx.Client(base_url=self.url, timeout=5) as client:
            while time.monotonic() < deadline:
                if not self._container_running():
                    raise RuntimeError(
                        "appliance container exited during boot; logs:\n"
                        f"{self.logs()}"
                    )
                try:
                    if client.get("/health").status_code == 200:
                        return
                except Exception as exc:  # not up yet
                    last_error = exc
                time.sleep(2.0)
        raise RuntimeError(
            f"appliance did not become healthy within {timeout}s "
            f"(last error: {last_error!r}); logs:\n{self.logs()}"
        )

    def _container_running(self) -> bool:
        result = _run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.name]
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    # --- control channel -----------------------------------------------

    def exec(
        self,
        *args: str,
        timeout: int = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """``docker exec`` inside the appliance (as the image's klangk user)."""
        result = _run(["docker", "exec", self.name, *args], timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"docker exec {args} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def exec_out(self, *args: str, timeout: int = 120) -> str:
        """``docker exec`` returning trimmed stdout (check on)."""
        return self.exec(*args, timeout=timeout).stdout.strip()

    def logs(self) -> str:
        return _run(["docker", "logs", self.name], timeout=60).stdout

    def stop(self) -> None:
        """Tear the appliance down (idempotent)."""
        _run(["docker", "rm", "-f", self.name], timeout=120)

    # --- service-state helpers -----------------------------------------

    def service_pids(self, pattern: str) -> list[str]:
        """PIDs inside the appliance matching a pgrep -f pattern."""
        result = self.exec("pgrep", "-f", pattern, check=False)
        return [pid for pid in result.stdout.split() if pid]
