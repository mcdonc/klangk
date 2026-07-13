"""End-to-end test: nginx dies when klangkd dies (#1439).

klangkd spawns nginx as a child. Before #1439, nginx ran in its own
session (``start_new_session=True``), so a hard kill of klangkd orphaned
it. After #1439, nginx runs in klangkd's process group and dies with it.

Run with: devenv shell -- test-backend-e2e test_nginx_lifecycle_e2e.py
"""

import os
import subprocess
import tempfile
import time

import httpx
import pytest

from klangk_backend.model import free_port
from _e2e_env import clean_env

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")


def _wait_for_nginx(port, timeout=30):
    """Wait until nginx accepts connections (any status is fine)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://localhost:{port}/", timeout=2)
            return True
        except httpx.ConnectError:
            pass
        time.sleep(0.3)
    return False


def _port_listening(port):
    """True if something is accepting connections on localhost:port."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def _start_klangkd():
    """Start a klangkd process with nginx enabled, return (proc, nginx_port)."""
    data_dir = tempfile.mkdtemp(prefix="klangk-nginx-lifecycle-")
    nginx_port = str(free_port())

    sock_path = os.path.join(data_dir, "klangk.sock")

    env = clean_env(
        KLANGK_LISTEN=sock_path,
        KLANGK_NGINX_PORT=nginx_port,
        KLANGK_STATE_DIR=data_dir,
        KLANGK_DATA_DIR=data_dir,
        KLANGK_JWT_SECRET="nginx-lifecycle-test",
        KLANGK_PREVENT_INSECURE_JWT_SECRET="",
        KLANGK_DEFAULT_USER="test@example.com",
        KLANGK_DEFAULT_PASSWORD="testpass",
        KLANGK_AUTH_MODES="none",
        KLANGK_TEST_MODE="1",
        KLANGK_IDLE_TIMEOUT_SECONDS="300",
        KLANGK_PORT_RANGE_START=str(free_port()),
        _KLANGK_DISABLE_NGINX="",
        LOGFIRE_TOKEN="",
    )

    proc = subprocess.Popen(
        ["python3", "-m", "klangk_backend.klangkd", "--config=none"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, nginx_port


class TestNginxDiesWithKlangkd:
    """nginx must not outlive klangkd (#1439)."""

    def test_nginx_stops_on_sigterm(self):
        """Graceful shutdown (SIGTERM) stops nginx."""
        proc, nginx_port = _start_klangkd()
        try:
            ok = _wait_for_nginx(nginx_port)
            if not ok:
                proc.kill()
                out, _ = proc.communicate(timeout=5)
                pytest.fail(
                    f"klangkd did not start:\n"
                    f"{out.decode(errors='replace')[:2000]}"
                )

            assert _port_listening(nginx_port), "nginx not serving"

            proc.terminate()
            proc.wait(timeout=10)
            time.sleep(1)

            assert not _port_listening(nginx_port), (
                "nginx port still listening after SIGTERM"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
