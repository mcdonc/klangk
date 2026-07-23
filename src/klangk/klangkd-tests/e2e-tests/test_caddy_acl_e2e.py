"""
E2E tests for the Caddy proxy engine: runtime ACL enforcement + parity
(#1559 Phase 2).

Caddy counterpart to ``test_proxy_acl_e2e.py`` (the nginx engine). These
tests spawn a real Caddy (from a rendered Caddyfile) and verify runtime
behavior that can only be proven with a live proxy:

* **ACL enforcement** — egress endpoints deny non-container source IPs.
* **Deny-by-default** — browser catch-all denies container source IPs
  (#1376), keyed on ``remote_ip`` not ``client_ip`` (#1546).
* **``/auth/local`` loopback gate** (#1374) — free-token endpoint is
  loopback-only.
* **Trusted-proxies real-client IP** (#1558) — ``X-Real-IP`` preserves the
  real client through an outer proxy hop.
* **``/hosted/`` proxying + WebSocket** — the Jupyter/Marimo path (#1237).
* **Secret never on disk** — the LLM API key, delivered via admin-API
  ``POST /load``, never lands in any file under ``state_dir``.

Config-file-level text assertions (the Caddyfile's directives) are already
covered by the unit suite (``test_caddy.py``); this file focuses on runtime
behavior. The config is delivered by writing the rendered Caddyfile to a
temp file and launching ``caddy run --config <file>`` — functionally
identical to the production admin-API path (the same Caddyfile is adapted
by Caddy either way); the secret-on-disk guarantee is tested separately by
starting full ``klangkd`` with ``KLANGKD_PROXY_ENGINE=caddy`` and scanning
``state_dir``.

Run with: devenv shell -- test-backend-e2e test_caddy_acl_e2e.py
"""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from klangk.model import free_port

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")


# ---------------------------------------------------------------------------
# Echo backend (returns JSON of received headers + path; 200 for everything)
# ---------------------------------------------------------------------------


class _EchoHandler(BaseHTTPRequestHandler):
    """Returns a 200 JSON echo of the request path + headers.

    Used as a dummy upstream so tests can verify proxy behavior (ACLs,
    header injection, routing) in isolation — the forward_auth subrequest
    also hits this and gets 200, so the auth gate passes and the ACL /
    route under test is the sole variable.
    """

    def _respond(self):
        body = json.dumps(
            {"path": self.path, "headers": dict(self.headers)}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def log_message(self, *args):
        pass


def _start_echo(port):
    """Start a threaded echo HTTP server on 127.0.0.1:<port>; return the server."""
    srv = HTTPServer(("127.0.0.1", port), _EchoHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _stop_echo(srv):
    srv.shutdown()
    srv.server_close()


def _start_ws_echo(port):
    """Start a WebSocket echo server on 127.0.0.1:<port> in a daemon thread.

    The server runs its own asyncio event loop so it can serve alongside the
    test's main thread. Each connection echoes every received text message back
    to the sender. Returns a state dict; stop with :func:`_stop_ws_echo`.
    """
    import asyncio

    import websockets

    loop = asyncio.new_event_loop()
    state: dict = {"loop": loop}

    async def _handler(websocket):
        async for msg in websocket:
            await websocket.send(msg)

    def _runner():
        asyncio.set_event_loop(loop)

        async def _setup():
            state["server"] = await websockets.serve(
                _handler, "127.0.0.1", port
            )

        loop.run_until_complete(_setup())
        loop.run_forever()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    state["thread"] = t
    # Wait briefly for the server to bind.
    import socket

    deadline = time.time() + 3
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            break
        except OSError:
            time.sleep(0.1)
    return state


def _stop_ws_echo(state):
    """Stop a WS echo server started by :func:`_start_ws_echo` cleanly."""
    import asyncio

    loop = state["loop"]
    server = state.get("server")

    async def _shutdown():
        if server is not None:
            server.close()
            await server.wait_closed()

    try:
        fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        fut.result(timeout=3)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    state["thread"].join(timeout=3)
    try:
        loop.close()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Caddy launch helpers
# ---------------------------------------------------------------------------


def _render_and_launch(settings_env, upstream, admin_sock, tmpdir):
    """Render the Caddyfile from ``settings_env`` and launch ``caddy run``.

    Returns ``(proc, conf_path)``. The Caddyfile is written to ``tmpdir``
    (NOT ``state_dir``) so the secret-on-disk tests' ``state_dir`` scan is
    not contaminated by the test harness.
    """
    import types

    from klangk.caddy import CaddyRenderer
    from klangk.settings import KlangkSettings

    settings = KlangkSettings(settings_env)
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    cf = CaddyRenderer(app).render_config(upstream, admin_sock)
    conf_path = os.path.join(tmpdir, "test.caddy")
    with open(conf_path, "w") as f:
        f.write(cf)

    # Clean any stale admin socket so the bind succeeds.
    try:
        os.unlink(admin_sock)
    except FileNotFoundError:
        pass

    proc = subprocess.Popen(
        ["caddy", "run", "--config", conf_path, "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, conf_path


def _wait_for_caddy(port, timeout=15):
    """Wait until Caddy accepts connections on localhost:<port>."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
            return True
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(0.3)
    return False


def _wait_for_caddy_health(port, timeout=15):
    """Wait until Caddy proxies /health to the backend and gets a 200."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(0.3)
    return False


def _host_nonloopback_ipv4():
    """A non-loopback IPv4 of this host — the source IP pasta NAT traffic
    appears as. Returns None when there is no suitable address (some CI
    sandboxes)."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None
    for line in out.splitlines():
        m = re.match(r"\s*inet (\d+\.\d+\.\d+\.\d+)/", line)
        if not m:
            continue
        ip = m.group(1)
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return ip
    return None


def _caddy_output(proc):
    """Drain Caddy's combined stdout/stderr for failure diagnostics."""
    try:
        return (proc.stdout.read() or b"").decode(errors="replace")[:2000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# ACL enforcement on the egress port
# ---------------------------------------------------------------------------


class TestCaddyAclEnforcement:
    """Egress endpoints deny non-container source IPs (CONTAINER_ACL).

    Uses an echo backend (TCP) so forward_auth passes (echo returns 200 for
    /api/v1/auth/verify-workspace-token), making the container ACL the sole
    blocking mechanism — isolating the ACL from the auth gate.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        tmpdir = str(tmp_path_factory.mktemp("caddy-acl"))
        echo_port = free_port()
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        echo = _start_echo(echo_port)
        try:
            from klangk.caddy import tcp_upstream

            settings_env = {
                "KLANGKD_DATA_DIR": tmpdir,
                "KLANGKD_STATE_DIR": tmpdir,
                "KLANGKD_PORT": str(browser_port),
                "KLANGKD_LISTEN": "127.0.0.1",
                "KLANGKD_EGRESS_PORT": str(egress_port),
                "KLANGKD_EGRESS_LISTEN": "127.0.0.1",
                "KLANGKD_CONTAINER_SUBNETS": "192.0.2.0/24",
                "KLANGKD_LLM_BASE_URL": f"http://127.0.0.1:{echo_port}",
                "KLANGKD_LLM_API_KEY": "fake-llm-key",
            }
            proc, conf_path = _render_and_launch(
                settings_env,
                tcp_upstream("127.0.0.1", str(echo_port)),
                admin_sock,
                tmpdir,
            )
            if not _wait_for_caddy_health(browser_port):
                proc.kill()
                pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

            yield {
                "browser_port": browser_port,
                "egress_port": egress_port,
                "echo_port": echo_port,
            }

            proc.terminate()
            proc.wait(timeout=5)
        finally:
            _stop_echo(echo)

    def test_llm_proxy_denied_from_non_container(self, stack):
        """From 127.0.0.1 (not in 192.0.2.0/24), /llm-proxy/ gets 403."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['egress_port']}/llm-proxy/v1/models",
            timeout=5,
        )
        assert r.status_code == 403

    def test_browser_delegate_denied_from_non_container(self, stack):
        """browser-delegate from non-container IP gets 403."""
        r = httpx.post(
            f"http://127.0.0.1:{stack['egress_port']}/api/v1/browser-delegate",
            timeout=5,
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Egress ACL allow-path (the positive case) (#1559 acceptance criterion)
# ---------------------------------------------------------------------------


class TestCaddyEgressAclAllow:
    """The egress ACL allows container-source peers through to the backend.

    The companion ``TestCaddyAclEnforcement`` exercises only the *deny*
    direction (non-container source → 403). This class proves the *allow*
    direction so a regression that flipped the matcher to deny-all (e.g. an
    empty ``remote_ip`` list, or a guard-ordering bug) would fail here, not
    silently pass while breaking every container in production.

    Technique (mirrors ``TestCaddyDenyByDefault``): set
    ``KLANGKD_CONTAINER_SUBNETS=<host_ip>`` so the host's non-loopback IPv4
    — the address pasta-NAT container traffic appears as — is a
    container source, then send from exactly that IP to the egress port
    (bound ``0.0.0.0`` so it's reachable via the host IP) and assert the
    request reaches the backend (200), not the ACL's 403.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        host_ip = _host_nonloopback_ipv4()
        if not host_ip:
            pytest.skip("no non-loopback IPv4 to simulate a container source")

        tmpdir = str(tmp_path_factory.mktemp("caddy-acl-allow"))
        echo_port = free_port()
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        echo = _start_echo(echo_port)
        try:
            from klangk.caddy import tcp_upstream

            settings_env = {
                "KLANGKD_DATA_DIR": tmpdir,
                "KLANGKD_STATE_DIR": tmpdir,
                "KLANGKD_PORT": str(browser_port),
                "KLANGKD_LISTEN": "127.0.0.1",
                "KLANGKD_EGRESS_PORT": str(egress_port),
                # Bind egress on all interfaces so the host_ip source is
                # reachable (127.0.0.1 would only accept loopback peers).
                "KLANGKD_EGRESS_LISTEN": "0.0.0.0",
                # The host's non-loopback IPv4 is the (sole) container source.
                "KLANGKD_CONTAINER_SUBNETS": host_ip,
                "KLANGKD_LLM_BASE_URL": f"http://127.0.0.1:{echo_port}",
                "KLANGKD_LLM_API_KEY": "fake-llm-key",
            }
            proc, conf_path = _render_and_launch(
                settings_env,
                tcp_upstream("127.0.0.1", str(echo_port)),
                admin_sock,
                tmpdir,
            )
            if not _wait_for_caddy_health(browser_port):
                proc.kill()
                pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

            yield {
                "egress_port": egress_port,
                "host_ip": host_ip,
            }

            proc.terminate()
            proc.wait(timeout=5)
        finally:
            _stop_echo(echo)

    def test_llm_proxy_allowed_from_container_ip(self, stack):
        """From the container source IP (host_ip), /llm-proxy/ passes the
        egress ACL and reaches the echo backend (200) — the ACL is
        source-IP-specific, not a blanket deny. A deny-all regression
        (empty matcher, guard mis-ordering) would make this 403."""
        r = httpx.get(
            f"http://{stack['host_ip']}:{stack['egress_port']}/llm-proxy/v1/models",
            timeout=5,
        )
        assert r.status_code == 200

    def test_browser_delegate_allowed_from_container_ip(self, stack):
        """From the container source IP, browser-delegate passes the egress
        ACL (reaches forward_auth → the echo backend returns 200). The
        positive counterpart to ``test_browser_delegate_denied_from_non_container``."""
        r = httpx.post(
            f"http://{stack['host_ip']}:{stack['egress_port']}/api/v1/browser-delegate",
            timeout=5,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Deny-by-default on the browser catch-all (#1376, #1546)
# ---------------------------------------------------------------------------


class TestCaddyDenyByDefault:
    """Runtime enforcement of deny-by-default from container source IPs.

    The browser catch-all denies the container source IPs (pasta NAT) via
    ``remote_ip`` (#1546: immediate TCP peer, not ``client_ip``) while
    allowing loopback (local browsers) and others (remote browsers).
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        host_ip = _host_nonloopback_ipv4()
        if not host_ip:
            pytest.skip("no non-loopback IPv4 to simulate a container source")

        from _e2e_server import start_server, stop_server

        tmpdir = str(tmp_path_factory.mktemp("caddy-deny-default"))
        data_dir = os.path.join(tmpdir, "data")
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        server = start_server(
            data_dir=data_dir,
            state_dir=state_dir,
            KLANGKD_JWT_SECRET="caddy-deny-test-secret",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        uds_path = server["uds_path"]

        from klangk.caddy import uds_upstream

        settings_env = {
            "KLANGKD_DATA_DIR": tmpdir,
            "KLANGKD_STATE_DIR": tmpdir,
            "KLANGKD_PORT": str(browser_port),
            "KLANGKD_LISTEN": "0.0.0.0",
            "KLANGKD_EGRESS_PORT": str(egress_port),
            "KLANGKD_EGRESS_LISTEN": "0.0.0.0",
            "KLANGKD_CONTAINER_SUBNETS": host_ip,
        }
        proc, conf_path = _render_and_launch(
            settings_env, uds_upstream(uds_path), admin_sock, tmpdir
        )
        if not _wait_for_caddy_health(browser_port):
            proc.kill()
            stop_server(server)
            pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

        yield {
            "browser_port": browser_port,
            "egress_port": egress_port,
            "host_ip": host_ip,
        }

        proc.terminate()
        proc.wait(timeout=5)
        stop_server(server)

    def test_api_denied_from_container_ip(self, stack):
        """From the container source IP, /api/v1/users is 403 — deny-by-default."""
        r = httpx.get(
            f"http://{stack['host_ip']}:{stack['browser_port']}/api/v1/users",
            timeout=5,
        )
        assert r.status_code == 403

    def test_proxied_request_with_container_xff_is_allowed(self, stack):
        """A request from loopback (trusted peer) whose X-Forwarded-For is a
        container-source IP must reach the backend (NOT 403). This is the
        #1546 fix: the guard keys on remote_ip (immediate peer), not
        client_ip (realip-rewritten)."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/users",
            headers={"X-Forwarded-For": stack["host_ip"]},
            timeout=5,
        )
        assert r.status_code != 403

    def test_api_allowed_from_loopback(self, stack):
        """From loopback, /api/v1/users reaches the backend (not 403)."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/users",
            timeout=5,
        )
        assert r.status_code != 403

    def test_health_from_loopback(self, stack):
        """Loopback browser traffic reaches the app."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/health", timeout=5
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /auth/local loopback ACL (#1374)
# ---------------------------------------------------------------------------


class TestCaddyAuthLocalAcl:
    """Runtime enforcement of the /api/v1/auth/local loopback gate (#1374).

    In ``none`` mode this endpoint freely issues an admin token, so the
    proxy's loopback-only ACL is what keeps a workspace container (which
    appears via pasta NAT as the host's non-loopback IP) from minting one.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        host_ip = _host_nonloopback_ipv4()
        if not host_ip:
            pytest.skip("no non-loopback IPv4 to simulate a container source")

        from _e2e_server import start_server, stop_server

        tmpdir = str(tmp_path_factory.mktemp("caddy-auth-local"))
        data_dir = os.path.join(tmpdir, "data")
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        server = start_server(
            data_dir=data_dir,
            state_dir=state_dir,
            KLANGKD_JWT_SECRET="caddy-auth-local-test-secret",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_AUTH_MODES="none",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        uds_path = server["uds_path"]

        from klangk.caddy import uds_upstream

        settings_env = {
            "KLANGKD_DATA_DIR": tmpdir,
            "KLANGKD_STATE_DIR": tmpdir,
            "KLANGKD_PORT": str(browser_port),
            "KLANGKD_LISTEN": "0.0.0.0",
            "KLANGKD_EGRESS_PORT": str(egress_port),
            "KLANGKD_EGRESS_LISTEN": "0.0.0.0",
        }
        proc, conf_path = _render_and_launch(
            settings_env, uds_upstream(uds_path), admin_sock, tmpdir
        )
        if not _wait_for_caddy_health(browser_port):
            proc.kill()
            stop_server(server)
            pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

        yield {"browser_port": browser_port, "host_ip": host_ip}

        proc.terminate()
        proc.wait(timeout=5)
        stop_server(server)

    def test_auth_local_denied_from_non_loopback(self, stack):
        """From the host's non-loopback IP, POST /auth/local is 403."""
        r = httpx.post(
            f"http://{stack['host_ip']}:{stack['browser_port']}/api/v1/auth/local",
            timeout=5,
        )
        assert r.status_code == 403

    def test_auth_local_allowed_from_loopback(self, stack):
        """From loopback, POST /auth/local reaches the backend (200) and
        mints a token — the auto-login path works."""
        r = httpx.post(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/auth/local",
            timeout=5,
        )
        assert r.status_code == 200
        assert "access_token" in r.json()


# ---------------------------------------------------------------------------
# Trusted-proxies real-client IP (#1558)
# ---------------------------------------------------------------------------


class TestCaddyTrustedProxies:
    """#1558: X-Real-IP preserves the real client through a trusted proxy hop.

    Caddy's ``servers { trusted_proxies static ... }`` makes ``{client_ip}``
    resolve the real client from ``X-Forwarded-For``. The ``header_up
    X-Real-IP {client_ip}`` then carries it to the backend. This test sends
    a request with ``X-Forwarded-For: <known-ip>`` from a trusted peer
    (loopback) and verifies the backend's echo sees ``X-Real-IP: <known-ip>``
    (not the proxy's loopback IP).

    Uses a minimal Caddyfile (not the full klangk config) to isolate the
    trusted_proxies mechanism from forward_auth / ACLs.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        tmpdir = str(tmp_path_factory.mktemp("caddy-trusted-proxy"))
        echo_port = free_port()
        proxy_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        echo = _start_echo(echo_port)
        try:
            # Minimal Caddyfile: trusted_proxies + X-Real-IP {client_ip},
            # no forward_auth / ACLs. This isolates the #1558 mechanism.
            caddyfile = (
                "{\n"
                "\tservers {\n"
                "\t\ttrusted_proxies static 127.0.0.1 ::1\n"
                "\t\ttrusted_proxies_strict\n"
                "\t}\n"
                "}\n"
                f"http://:{proxy_port} {{\n"
                "\tbind 127.0.0.1\n"
                f"\treverse_proxy 127.0.0.1:{echo_port} {{\n"
                "\t\theader_up X-Real-IP {client_ip}\n"
                "\t}\n"
                "}\n"
            )
            conf_path = os.path.join(tmpdir, "test.caddy")
            with open(conf_path, "w") as f:
                f.write(caddyfile)
            try:
                os.unlink(admin_sock)
            except FileNotFoundError:
                pass
            proc = subprocess.Popen(
                [
                    "caddy",
                    "run",
                    "--config",
                    conf_path,
                    "--adapter",
                    "caddyfile",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if not _wait_for_caddy(proxy_port):
                proc.kill()
                pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

            yield {"proxy_port": proxy_port, "echo_port": echo_port}

            proc.terminate()
            proc.wait(timeout=5)
        finally:
            _stop_echo(echo)

    def test_real_client_ip_preserved(self, stack):
        """A request with X-Forwarded-For from a trusted peer (loopback)
        makes the backend see X-Real-IP = the forwarded IP, not 127.0.0.1."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['proxy_port']}/test",
            headers={"X-Forwarded-For": "203.0.113.42"},
            timeout=5,
        )
        assert r.status_code == 200
        echoed = r.json()
        assert echoed["headers"].get("X-Real-Ip") == "203.0.113.42"

    def test_no_xff_falls_back_to_peer(self, stack):
        """Without X-Forwarded-For, X-Real-IP is the immediate peer (127.0.0.1)."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['proxy_port']}/test", timeout=5
        )
        assert r.status_code == 200
        echoed = r.json()
        assert echoed["headers"].get("X-Real-Ip") == "127.0.0.1"


# ---------------------------------------------------------------------------
# /hosted/ proxying + WebSocket (#1237)
# ---------------------------------------------------------------------------


class TestCaddyHostedProxy:
    """The /hosted/<ws>/<port>/ proxy: 308 redirect + trailing-slash proxying.

    WebSocket upgrade is automatic in Caddy's reverse_proxy — tested via a
    real WS round-trip.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        tmpdir = str(tmp_path_factory.mktemp("caddy-hosted"))
        echo_port = free_port()
        hosted_http_port = free_port()  # HTTP echo for the trailing-slash test
        hosted_ws_port = free_port()  # WS echo for the WebSocket test
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        echo = _start_echo(echo_port)
        hosted_echo = _start_echo(hosted_http_port)
        ws_state = _start_ws_echo(hosted_ws_port)
        try:
            from klangk.caddy import tcp_upstream

            settings_env = {
                "KLANGKD_DATA_DIR": tmpdir,
                "KLANGKD_STATE_DIR": tmpdir,
                "KLANGKD_PORT": str(browser_port),
                "KLANGKD_LISTEN": "127.0.0.1",
                "KLANGKD_EGRESS_PORT": str(egress_port),
                "KLANGKD_EGRESS_LISTEN": "127.0.0.1",
                "KLANGKD_CONTAINER_SUBNETS": "192.0.2.0/24",
            }
            proc, conf_path = _render_and_launch(
                settings_env,
                tcp_upstream("127.0.0.1", str(echo_port)),
                admin_sock,
                tmpdir,
            )
            if not _wait_for_caddy_health(browser_port):
                proc.kill()
                pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

            yield {
                "browser_port": browser_port,
                "hosted_http_port": hosted_http_port,
                "hosted_ws_port": hosted_ws_port,
            }

            proc.terminate()
            proc.wait(timeout=5)
        finally:
            _stop_echo(echo)
            _stop_echo(hosted_echo)
            _stop_ws_echo(ws_state)

    def test_hosted_slashless_redirects_308(self, stack):
        """Slash-less /hosted/<ws>/<port> 308-redirects to trailing-slash."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/hosted/ws1/{stack['hosted_http_port']}",
            timeout=5,
            follow_redirects=False,
        )
        assert r.status_code == 308
        assert (
            r.headers["location"]
            .rstrip("/")
            .endswith(f"/hosted/ws1/{stack['hosted_http_port']}")
        )

    def test_hosted_trailing_slash_proxies(self, stack):
        """Trailing-slash /hosted/<ws>/<port>/path proxies to the local port."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/hosted/ws1/{stack['hosted_http_port']}/some/path",
            timeout=5,
        )
        assert r.status_code == 200
        # The hosted proxy strips the /hosted/<ws>/<port>/ prefix and proxies
        # to 127.0.0.1:<port>, so the echo backend sees /some/path.
        body = r.json()
        assert body["path"] == "/some/path"

    def test_hosted_websocket_upgrade(self, stack):
        """WebSocket through the /hosted/ proxy round-trips a message.

        Caddy's reverse_proxy auto-upgrades the WebSocket; this verifies a
        real message round-trips end-to-end through the hosted proxy path —
        the Jupyter/Marimo interactive path (#1237).
        """
        import asyncio

        import websockets

        async def _round_trip():
            ws_base = f"ws://127.0.0.1:{stack['browser_port']}"
            async with websockets.connect(
                f"{ws_base}/hosted/ws1/{stack['hosted_ws_port']}/"
            ) as ws:
                await ws.send("hello-caddy")
                return await asyncio.wait_for(ws.recv(), timeout=5)

        reply = asyncio.run(_round_trip())
        assert reply == "hello-caddy"


# ---------------------------------------------------------------------------
# Secret never on disk + admin UDS-only (#1559 acceptance criteria)
# ---------------------------------------------------------------------------


class TestCaddySecretNotOnDisk:
    """The LLM API key, delivered via admin-API POST /load, never lands in any
    file under state_dir. And the admin endpoint is a UDS only (no loopback
    TCP).

    This starts the full ``klangkd`` with ``KLANGKD_PROXY_ENGINE=caddy`` and
    an LLM API key, waits for Caddy to serve, then scans every file under
    state_dir for the key string. The CaddyWatchdog delivers the Caddyfile
    via ``POST /load`` (in-memory) and ``persist_config off`` prevents
    Caddy's autosave, so the key should be absent from all files.
    """

    def test_llm_api_key_not_in_state_dir(self, tmp_path):
        """The LLM API key string is absent from every file Caddy could persist.

        Scans ``state_dir`` (where klangk writes its own state) AND Caddy's
        autosave path (``$XDG_CONFIG_HOME/caddy/autosave.json``, or
        ``~/.config/caddy/autosave.json``). The renderer sets
        ``persist_config off``, which suppresses autosave — so a regression
        that dropped that directive would leak the key into the autosave file
        (confirmed empirically: Caddy writes the full adapted config, key
        included, there). Scanning both paths catches that regression; scanning
        only ``state_dir`` would miss it (autosave lives outside state_dir).
        """
        from _e2e_env import clean_env, close_popen_pipes

        llm_key = "caddy-secret-key-7f3a9b2e"
        state_dir = str(tmp_path / "state")
        data_dir = str(tmp_path / "data")
        os.makedirs(state_dir)
        os.makedirs(data_dir)
        browser_port = free_port()
        egress_port = free_port()

        env = clean_env(
            KLANGKD_PROXY_ENGINE="caddy",
            KLANGKD_PORT=str(browser_port),
            KLANGKD_LISTEN="127.0.0.1",
            KLANGKD_EGRESS_PORT=str(egress_port),
            KLANGKD_EGRESS_LISTEN="127.0.0.1",
            KLANGKD_STATE_DIR=state_dir,
            KLANGKD_DATA_DIR=data_dir,
            KLANGKD_JWT_SECRET="caddy-secret-test",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_AUTH_MODES="none",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            KLANGKD_PORT_RANGE_START=str(free_port()),
            KLANGKD_LLM_BASE_URL="http://127.0.0.1:9999",
            KLANGKD_LLM_API_KEY=llm_key,
            _KLANGKD_DISABLE_PROXY="",
            LOGFIRE_TOKEN="",
        )
        # Resolve Caddy's autosave path the way Caddy does: XDG_CONFIG_HOME
        # (or ~/.config) + /caddy/autosave.json. Capture before launch so a
        # pre-existing autosave from a prior Caddy run is scanned too.
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        autosave_paths = [
            os.path.join(xdg_cfg, "caddy", "autosave.json"),
        ]

        proc = subprocess.Popen(
            ["python3", "-m", "klangk.launcher", "--config=none"],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            if not _wait_for_caddy_health(browser_port, timeout=30):
                proc.kill()
                pytest.fail(
                    f"klangkd+caddy did not start:\n{_caddy_output(proc)}"
                )

            # Give Caddy a moment to settle (any autosave would happen now).
            time.sleep(2)

            # Build the full scan list: every regular file under state_dir,
            # plus Caddy's autosave path(s) (which live outside state_dir).
            scan_paths = []
            for root, _dirs, files in os.walk(state_dir):
                for fname in files:
                    scan_paths.append(os.path.join(root, fname))
            scan_paths.extend(autosave_paths)

            found_in = []
            for fpath in scan_paths:
                try:
                    with open(fpath, "rb") as f:
                        if llm_key.encode() in f.read():
                            found_in.append(fpath)
                except OSError:
                    pass
            assert not found_in, f"LLM API key found on disk in: {found_in}"
        finally:
            proc.kill()
            proc.wait(timeout=5)
            close_popen_pipes(proc)
            # Best-effort: remove a test-created autosave so a later run
            # starts clean (and so the scan above isn't tripped by a stale
            # file from a *previous* failed run on the same host).
            for p in autosave_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_admin_endpoint_is_uds_only(self, tmp_path):
        """No loopback TCP listener on Caddy's default admin port (:2019).

        The rendered Caddyfile's ``admin unix//...|0600`` moves the admin
        endpoint to a UDS, so the default loopback :2019 must not be open.
        """
        import socket

        tmpdir = str(tmp_path)
        echo_port = free_port()
        browser_port = free_port()
        egress_port = free_port()
        admin_sock = os.path.join(tmpdir, "caddy-admin.sock")

        echo = _start_echo(echo_port)
        try:
            from klangk.caddy import tcp_upstream

            settings_env = {
                "KLANGKD_DATA_DIR": tmpdir,
                "KLANGKD_STATE_DIR": tmpdir,
                "KLANGKD_PORT": str(browser_port),
                "KLANGKD_LISTEN": "127.0.0.1",
                "KLANGKD_EGRESS_PORT": str(egress_port),
                "KLANGKD_EGRESS_LISTEN": "127.0.0.1",
                "KLANGKD_CONTAINER_SUBNETS": "192.0.2.0/24",
            }
            proc, conf_path = _render_and_launch(
                settings_env,
                tcp_upstream("127.0.0.1", str(echo_port)),
                admin_sock,
                tmpdir,
            )
            try:
                if not _wait_for_caddy_health(browser_port):
                    proc.kill()
                    pytest.fail(f"Caddy did not start:\n{_caddy_output(proc)}")

                # The admin UDS must exist.
                assert os.path.exists(admin_sock), (
                    f"admin UDS not found at {admin_sock}"
                )

                # Caddy's default loopback admin (:2019) must NOT be open.
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                try:
                    sock.connect(("127.0.0.1", 2019))
                    open_2019 = True
                except (ConnectionRefusedError, OSError):
                    open_2019 = False
                finally:
                    sock.close()
                assert not open_2019, (
                    "Caddy's default loopback admin :2019 is open — the "
                    "admin UDS directive did not take effect"
                )
            finally:
                proc.terminate()
                proc.wait(timeout=5)
        finally:
            _stop_echo(echo)
