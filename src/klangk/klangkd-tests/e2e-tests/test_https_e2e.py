"""E2E tests for HTTPS serving with automatic TLS armed (#3192).

Real-ACME issuance cannot run in CI (it needs a public DNS record and
reachable ports 80/443), so the serving tests arm the **internal
issuer** — ``KLANGKD_TLS_ISSUER=internal``, a first-class production
mode (#3192): Caddy self-generates the key + certificate for the armed
name at runtime (the same listener/handshake/proxying surface ACME
would produce, without a CA) and disables the HTTP→HTTPS redirect
(whose :80 bind unprivileged CI runners cannot make). The rendered
config is therefore the untouched production output.

The harness also makes three small substitutions around the production
machinery: a TCP test upstream for the backend UDS dial, ``_task``
pre-set so reloads are not short-circuited (no ``_watch`` loop runs),
and — in the SIGHUP test's ACME-mode config only — an injected
``auto_https disable_redirects`` so the ACME automation policy loads on
unprivileged runners (issuance itself fails offline, harmlessly).

What is covered:

* **Armed HTTPS serving** — TLS handshake with a self-generated key, the
  served certificate's SAN matches the armed ``KLANGKD_TLS_HOSTNAME``,
  requests proxy to the backend with ``X-Forwarded-Proto: https``, the
  wss WebSocket upgrade works through the HTTPS listener, the egress
  listener stays plain HTTP, and certificate material persists under
  ``<state_dir>/caddy-storage`` (the explicit storage path).
* **``KLANGKD_HOSTING_HOSTNAME`` + ``KLANGKD_TLS_HOSTNAME`` together** —
  the listener serves the TLS name while URL generation honors the
  hosting pin (and derives from headers when unpinned).
* **SIGHUP updates the certificate configuration** — the real
  ``reconfigure()`` + ``apply_pending_reload()`` flow swaps the armed
  FQDN + ACME email in the **running** caddy's active config, and a
  disarm drops the TLS machinery — no process restart.

Run with: devenv shell -- test-backend-e2e test_https_e2e.py
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import types

import httpx
import pytest

from klangk.caddy import CaddyRenderer, CaddyWatchdog, tcp_upstream
from klangk.model import free_port
from klangk.settings import KlangkSettings
from klangk.util import Util

pytestmark = pytest.mark.skipif(
    shutil.which("caddy") is None, reason="no caddy binary on PATH"
)

FQDN = "https-host.example.com"
PIN_AUTHORITY = "portal.example.com:9000"


# ---------------------------------------------------------------------------
# Settings / renderer helpers
# ---------------------------------------------------------------------------


def _armed_settings(
    state: str, browser_port: int, fqdn: str = FQDN, **extra: str
) -> KlangkSettings:
    """Full/browser-mode settings with automatic TLS armed for *fqdn*."""
    env = {
        "KLANGKD_STATE_DIR": state,
        "KLANGKD_DATA_DIR": os.path.join(state, "data"),
        "KLANGKD_LISTEN": "127.0.0.1",
        "KLANGKD_PORT": str(browser_port),
        "KLANGKD_EGRESS_PORT": str(free_port()),
        "KLANGKD_TLS_HOSTNAME": fqdn,
    }
    env.update(extra)
    return KlangkSettings(env)


class _NoRedirectsRenderer(CaddyRenderer):
    """ACME-mode armed renders + ``auto_https disable_redirects``.

    The only test-only substitution left (#3192): the automatic
    HTTP→HTTPS redirect binds :80 at config-load time, which
    unprivileged runners cannot bind. Internal-issuer renders need no
    substitution at all — that mode disables the redirect natively.
    """

    def render_config(self, upstream, admin_socket, *, full_global=True):
        cf = super().render_config(
            upstream, admin_socket, full_global=full_global
        )
        if self.auto_https_armed and not self._internal_tls():
            cf = cf.replace("{\n", "{\n\tauto_https disable_redirects\n", 1)
        return cf


# ---------------------------------------------------------------------------
# Caddy child + admin API helpers
# ---------------------------------------------------------------------------


def _admin_get(admin_socket: str) -> dict:
    """GET the running caddy's active config over the admin UDS."""
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=admin_socket), timeout=5
    ) as c:
        r = c.get("http://localhost/config/")
        r.raise_for_status()
        return r.json()


def _wait_admin_socket(admin_socket: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(admin_socket)
            return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.1)
    return False


class _TcpUpstreamWatchdog(CaddyWatchdog):
    """The real watchdog, rendering to a TCP test upstream.

    The production ``_render_caddyfile`` dials the backend UDS; e2e backends
    here are TCP echo servers, so only the dial target is swapped. The
    SIGHUP reload path (``reconfigure`` + ``apply_pending_reload`` →
    ``load_config`` → ``_render_caddyfile``) runs through this same
    override, so reload tests exercise the real flow.
    """

    def __init__(self, app, renderer: CaddyRenderer) -> None:
        super().__init__(app)
        self._renderer = renderer
        self._upstream_port = 0

    def point_at(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port

    def _render_caddyfile(self) -> str:
        return self._renderer.render_config(
            tcp_upstream("127.0.0.1", self._upstream_port),
            self.admin_socket,
            full_global=True,
        )


class _CaddyChild:
    """A caddy child bootstrapped exactly the way the watchdog does it.

    Bootstrap config (admin UDS only) on disk, then the real config is
    delivered via the production ``CaddyWatchdog.load_config`` /
    ``post_load`` path — the same ``POST /load`` the lifespan and SIGHUP
    flows use.
    """

    def __init__(self, settings, renderer_cls=CaddyRenderer) -> None:
        self.settings = settings
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        self.admin_socket = settings.caddy_admin_socket
        self.state_dir = settings.state_dir
        self._renderer = renderer_cls(self.app)
        self.proc: subprocess.Popen | None = None
        self.watchdog: CaddyWatchdog | None = None

    def start(self) -> None:
        bootstrap = os.path.join(self.state_dir, "caddy-bootstrap.Caddyfile")
        with open(bootstrap, "w") as f:
            f.write(
                CaddyRenderer(self.app)._bootstrap_block(self.admin_socket)
            )
        try:
            os.unlink(self.admin_socket)
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen(
            [
                "caddy",
                "run",
                "--config",
                bootstrap,
                "--adapter",
                "caddyfile",
            ],
            # DEVNULL: nothing drains the pipe in this harness, and the
            # ACME-mode reload test makes caddy emit issuance-failure
            # noise — a full pipe would wedge the child.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_admin_socket(self.admin_socket):
            self.stop()
            pytest.fail("caddy admin UDS never came up")

    def make_watchdog(self) -> _TcpUpstreamWatchdog:
        """The real watchdog pointed at this child (test renderer swapped
        in; ``_task`` set so reloads are not short-circuited)."""
        wd = _TcpUpstreamWatchdog(self.app, self._renderer)
        wd._full_global = True
        wd._task = object()
        self.watchdog = wd
        return wd

    async def load(self, upstream_port: int) -> CaddyWatchdog:
        """Deliver the armed config over the production admin-API path."""
        wd = self.make_watchdog()
        wd.point_at(upstream_port)
        await wd.load_config()
        return wd

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            os.unlink(self.admin_socket)
        except FileNotFoundError:
            pass


@pytest.fixture
def caddy_factory():
    """Start armed caddy children; stop them all on teardown."""
    children: list[_CaddyChild] = []

    def _start(settings, renderer_cls=CaddyRenderer) -> _CaddyChild:
        child = _CaddyChild(settings, renderer_cls=renderer_cls)
        child.start()
        children.append(child)
        return child

    yield _start
    for child in children:
        child.stop()


# ---------------------------------------------------------------------------
# Echo backends (HTTP + WebSocket)
# ---------------------------------------------------------------------------


def _start_http_echo(port: int):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
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

    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _start_ws_echo(port: int):
    import asyncio

    import websockets

    loop = asyncio.new_event_loop()
    state: dict = {"loop": loop}

    async def _handler(ws):
        async for msg in ws:
            await ws.send(msg)

    def _runner():
        asyncio.set_event_loop(loop)

        async def _setup():
            state["server"] = await websockets.serve(
                _handler, "127.0.0.1", port
            )

        loop.run_until_complete(_setup())
        loop.run_forever()

    state["thread"] = threading.Thread(target=_runner, daemon=True)
    state["thread"].start()
    # Wait for the listener to bind (server object + sockets appear once
    # _setup ran) — no TCP probe, which would log a handshake error.
    deadline = time.time() + 3
    while time.time() < deadline:
        server = state.get("server")
        if server is not None and server.sockets:
            break
        time.sleep(0.05)
    return state


def _stop_ws_echo(state: dict) -> None:
    import asyncio

    loop = state.get("loop")
    if loop is None:
        return
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
# TLS client helpers
# ---------------------------------------------------------------------------


def _insecure_ctx() -> ssl.SSLContext:
    """Verification off (self-generated test certs) but TLS 1.2+ pinned —
    the floor caddy serves, so handshakes prove modern TLS."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _https_get(port: int, fqdn: str, path: str = "/") -> tuple:
    """Raw HTTPS/1.1 GET with the right SNI; returns (cert_der, body)."""
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    s = _insecure_ctx().wrap_socket(raw, server_hostname=fqdn)
    try:
        cert_der = s.getpeercert(binary_form=True)
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {fqdn}:{port}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        s.close()
    resp = b"".join(chunks)
    assert b" 200 " in resp.split(b"\r\n", 1)[0], resp[:400]
    body = resp.split(b"\r\n\r\n", 1)[1]
    return cert_der, json.loads(body)


def _cert_dns_sans(cert_der: bytes) -> list[str]:
    from cryptography import x509

    cert = x509.load_der_x509_certificate(cert_der)
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        return list(san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        return []


def _wait_tls_ready(port: int, fqdn: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = socket.create_connection(("127.0.0.1", port), timeout=1)
            s = _insecure_ctx().wrap_socket(raw, server_hostname=fqdn)
            s.close()
            return
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    pytest.fail(f"TLS listener never came up on {port}: {last}")


def _patch_dns(monkeypatch, fqdn: str) -> None:
    """Resolve *fqdn* to loopback so URL-based clients (websockets) dial
    the local listener while keeping the real hostname for SNI/Host."""
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host == fqdn:
            return real("127.0.0.1", port, *args, **kwargs)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArmedHttpsServing:
    async def test_self_generated_key_serves_https(
        self, tmp_path, caddy_factory
    ):
        """Armed + internal (self-generated) key: handshake, SAN, proxying,
        storage persistence, plain-HTTP egress."""
        import asyncio

        state = str(tmp_path)
        port = free_port()
        settings = _armed_settings(
            state, port, **{"KLANGKD_TLS_ISSUER": "internal"}
        )
        echo_port = free_port()
        echo = _start_http_echo(echo_port)
        child = caddy_factory(settings)
        try:
            await child.load(echo_port)
            await asyncio.to_thread(_wait_tls_ready, port, FQDN)

            # The served certificate is for the armed FQDN, self-generated
            # by caddy's internal issuer at runtime.
            cert_der, body = _https_get(port, FQDN)
            assert FQDN in _cert_dns_sans(cert_der)

            # The request proxied to the backend as HTTPS.
            headers = {k.lower(): v for k, v in body["headers"].items()}
            assert headers["x-forwarded-proto"] == "https"
            assert headers["host"] == FQDN  # {host} carries no port

            # Certificate material persisted through the explicit storage
            # path under state_dir (survives restarts, #3192 criterion).
            storage = os.path.join(state, "caddy-storage")
            assert os.path.isdir(storage) and os.listdir(storage)

            # The active config carries the armed identity.
            cfg = _admin_get(settings.caddy_admin_socket)
            policies = cfg["apps"]["tls"]["automation"]["policies"]
            assert policies and policies[0]["subjects"] == [FQDN]
            assert cfg["storage"]["root"] == storage

            # The container-egress listener stays plain HTTP.
            r = httpx.get(
                f"http://127.0.0.1:{settings.egress_port}/llm-proxy/x",
                timeout=5,
            )
            assert r.status_code == 200
        finally:
            echo.shutdown()
            echo.server_close()

    async def test_wss_upgrade_through_https_listener(
        self, tmp_path, monkeypatch, caddy_factory
    ):
        """The WebSocket upgrade works over TLS (wss) on the armed
        listener — the terminal path browsers use in HTTPS deployments."""
        import asyncio

        import websockets

        state = str(tmp_path)
        port = free_port()
        settings = _armed_settings(
            state, port, **{"KLANGKD_TLS_ISSUER": "internal"}
        )
        ws_port = free_port()
        ws = _start_ws_echo(ws_port)
        child = caddy_factory(settings)
        try:
            await child.load(ws_port)
            await asyncio.to_thread(_wait_tls_ready, port, FQDN)
            _patch_dns(monkeypatch, FQDN)
            async with websockets.connect(
                f"wss://{FQDN}:{port}/ws",
                ssl=_insecure_ctx(),
                open_timeout=10,
            ) as conn:
                await conn.send("hello over tls")
                assert await conn.recv() == "hello over tls"
        finally:
            _stop_ws_echo(ws)


class TestHostingHostnameWithTlsHostname:
    async def test_pin_drives_urls_tls_name_drives_listener(
        self, tmp_path, caddy_factory
    ):
        """Both set: the listener + certificate use KLANGKD_TLS_HOSTNAME
        while URL generation honors the KLANGKD_HOSTING_HOSTNAME pin."""
        import asyncio

        state = str(tmp_path)
        port = free_port()
        settings = _armed_settings(
            state,
            port,
            **{
                "KLANGKD_TLS_ISSUER": "internal",
                "KLANGKD_HOSTING_HOSTNAME": PIN_AUTHORITY,
            },
        )
        echo_port = free_port()
        echo = _start_http_echo(echo_port)
        child = caddy_factory(settings)
        try:
            await child.load(echo_port)
            await asyncio.to_thread(_wait_tls_ready, port, FQDN)

            # Listener identity: the TLS name, not the hosting pin.
            cert_der, body = _https_get(port, FQDN)
            assert FQDN in _cert_dns_sans(cert_der)
            headers = {k.lower(): v for k, v in body["headers"].items()}
            assert headers["host"] == FQDN  # {host} carries no port

            # URL generation: the pin wins over the very same request's
            # Host header (real Util, real settings, trusted loopback peer).
            util = Util(
                types.SimpleNamespace(
                    state=types.SimpleNamespace(settings=settings)
                )
            )
            pin_host, pin_proto, _ = util.derive_hosting_info(
                {"host": f"{FQDN}:{port}", "x-forwarded-proto": "https"},
                client_host="127.0.0.1",
            )
            assert pin_host == PIN_AUTHORITY
            assert pin_proto == "https"

            # Without the pin, the same headers derive the TLS identity —
            # the zero-extra-config automatic-TLS URL behavior.
            unpinned = _armed_settings(
                state, port, **{"KLANGKD_TLS_ISSUER": "internal"}
            )
            util2 = Util(
                types.SimpleNamespace(
                    state=types.SimpleNamespace(settings=unpinned)
                )
            )
            host2, proto2, _ = util2.derive_hosting_info(
                {"host": f"{FQDN}:{port}", "x-forwarded-proto": "https"},
                client_host="127.0.0.1",
            )
            assert host2 == f"{FQDN}:{port}"
            assert proto2 == "https"
        finally:
            echo.shutdown()
            echo.server_close()


class TestSighupUpdatesCertConfiguration:
    async def test_reload_swaps_tls_config_in_running_caddy(
        self, tmp_path, caddy_factory
    ):
        """The real SIGHUP path — reconfigure() flags, apply_pending_reload()
        pushes — swaps FQDN + ACME email in the running caddy's active
        config, then a disarm drops the TLS machinery. No restart."""

        def emails(cfg):
            return {
                i.get("email")
                for p in cfg["apps"]["tls"]["automation"]["policies"]
                for i in p.get("issuers", [])
            }

        def subjects(cfg):
            return [
                s
                for p in cfg["apps"]["tls"]["automation"]["policies"]
                for s in p.get("subjects", [])
            ]

        state = str(tmp_path)
        port = free_port()
        v1 = _armed_settings(
            state,
            port,
            fqdn="alpha.example.com",
            **{"KLANGKD_ACME_EMAIL": "one@example.com"},
        )
        # ACME mode: the automation policy + email are the thing under
        # test (issuance itself fails offline — harmless); the renderer
        # subclass only suppresses the :80 redirect bind.
        child = caddy_factory(v1, renderer_cls=_NoRedirectsRenderer)
        wd = child.make_watchdog()
        try:
            wd.point_at(free_port())
            await wd.load_config()
            cfg = _admin_get(v1.caddy_admin_socket)
            assert subjects(cfg) == ["alpha.example.com"]
            assert emails(cfg) == {"one@example.com"}

            # SIGHUP: new FQDN + new ACME email on the RUNNING child.
            v2 = _armed_settings(
                state,
                port,
                fqdn="beta.example.com",
                **{"KLANGKD_ACME_EMAIL": "two@example.com"},
            )
            wd.reconfigure(
                types.SimpleNamespace(state=types.SimpleNamespace(settings=v2))
            )
            assert wd._pending_reload is True
            await wd.apply_pending_reload()
            cfg = _admin_get(v2.caddy_admin_socket)
            assert subjects(cfg) == ["beta.example.com"]
            assert emails(cfg) == {"two@example.com"}
            assert child.proc.poll() is None  # same process, no restart

            # SIGHUP again: disarm — the TLS machinery leaves the config.
            v3 = KlangkSettings(
                {
                    "KLANGKD_STATE_DIR": state,
                    "KLANGKD_DATA_DIR": os.path.join(state, "data"),
                    "KLANGKD_LISTEN": "127.0.0.1",
                    "KLANGKD_PORT": str(port),
                    "KLANGKD_EGRESS_PORT": str(free_port()),
                }
            )
            wd.reconfigure(
                types.SimpleNamespace(state=types.SimpleNamespace(settings=v3))
            )
            await wd.apply_pending_reload()
            cfg = _admin_get(v3.caddy_admin_socket)
            assert (
                not cfg["apps"]
                .get("tls", {})
                .get("automation", {})
                .get("policies")
            )
            assert child.proc.poll() is None
        finally:
            child.stop()
