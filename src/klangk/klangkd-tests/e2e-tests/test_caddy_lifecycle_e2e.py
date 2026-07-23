"""End-to-end test: the Caddy engine dies when klangkd dies (#1439, #1533, #1559).

Caddy counterpart to ``test_proxy_lifecycle_e2e.py`` (the nginx engine).
klangkd spawns the proxy in its own session (``setsid`` via ``preexec_fn``)
with ``PR_SET_PDEATHSIG(SIGTERM)`` so the kernel auto-signals the proxy when
klangkd exits. ``stop()`` uses ``os.killpg`` for clean shutdown. The
combination ensures the proxy dies with klangkd under SIGTERM, SIGINT, and
SIGKILL.

The Caddy engine (``CaddyWatchdog``) reuses the nginx engine's preexec
(``_proxy_preexec``), so the session/PDEATHSIG/killpg behavior is shared —
but it must be proven against a real ``caddy`` child. These tests do that:
they spawn the full ``klangkd`` with ``KLANGKD_PROXY_ENGINE=caddy`` and send
each signal, asserting the proxy's port closes.

Run with: devenv shell -- test-backend-e2e test_caddy_lifecycle_e2e.py
"""

import os
import signal
import subprocess
import tempfile
import time

import httpx
import pytest

from klangk.model import free_port
from _e2e_env import clean_env, close_popen_pipes

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")


def _wait_for_proxy(port, timeout=30):
    """Wait until the proxy accepts connections (any status is fine)."""
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
    """Start a klangkd process with the Caddy proxy engine, return (proc, port)."""
    data_dir = tempfile.mkdtemp(prefix="klangk-caddy-lifecycle-")
    egress_port = str(free_port())

    env = clean_env(
        KLANGKD_PROXY_ENGINE="caddy",
        KLANGKD_PORT=str(free_port()),
        KLANGKD_EGRESS_PORT=egress_port,
        KLANGKD_STATE_DIR=data_dir,
        KLANGKD_DATA_DIR=data_dir,
        KLANGKD_JWT_SECRET="caddy-lifecycle-test",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_AUTH_MODES="none",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_PORT_RANGE_START=str(free_port()),
        _KLANGKD_DISABLE_PROXY="",
        LOGFIRE_TOKEN="",
    )

    proc = subprocess.Popen(
        ["python3", "-m", "klangk.launcher", "--config=none"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, egress_port


def _wait_for_port_closed(port, timeout=10):
    """Wait until nothing is listening on localhost:port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_listening(port):
            return True
        time.sleep(0.3)
    return False


def _signal_test(sig):
    """Start klangkd + the Caddy proxy, send *sig*, assert the proxy port closes."""
    proc, egress_port = _start_klangkd()
    try:
        ok = _wait_for_proxy(egress_port)
        if not ok:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            pytest.fail(
                f"klangkd did not start:\n"
                f"{out.decode(errors='replace')[:2000]}"
            )

        assert _port_listening(egress_port), "caddy proxy not serving"

        os.kill(proc.pid, sig)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        assert _wait_for_port_closed(egress_port), (
            f"caddy proxy port still listening after {signal.Signals(sig).name}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        close_popen_pipes(proc)


class TestCaddyDiesWithKlangkd:
    """The Caddy proxy must not outlive klangkd (#1439, #1533, #1559).

    The CaddyWatchdog reuses the nginx preexec (setsid + PR_SET_PDEATHSIG),
    so the behavior is shared — these tests prove it against a real Caddy
    child, not just by code inspection.
    """

    def test_caddy_stops_on_sigterm(self):
        """Graceful shutdown (SIGTERM) stops the Caddy proxy."""
        _signal_test(signal.SIGTERM)

    def test_caddy_stops_on_sigint(self):
        """Keyboard interrupt (SIGINT) stops the Caddy proxy."""
        _signal_test(signal.SIGINT)

    def test_caddy_stops_on_sigkill(self):
        """Hard kill (SIGKILL) — PR_SET_PDEATHSIG fires, the Caddy proxy exits."""
        _signal_test(signal.SIGKILL)
