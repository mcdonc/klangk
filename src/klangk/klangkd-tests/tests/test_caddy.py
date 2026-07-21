"""Unit tests for the Caddy proxy engine (#1559).

Parallel to ``test_proxy.py`` (the nginx engine). These exercise the pure
Caddyfile rendering logic + the admin-API client / watchdog orchestration
without a running Caddy — the runtime enforcement (spawn/respawn, ACLs,
forward_auth) is covered by the e2e suite (``test_caddy_*_e2e.py``, run under
devenv where the ``caddy`` binary is present; CI's plain-pip unit job has no
caddy, so nothing here shells out to it).
"""

import asyncio
import os
import signal
import types
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from klangk.caddy import (
    CaddyRenderer,
    CaddyWatchdog,
)
from klangk.caddy import (
    CADDYFILE_CONTENT_TYPE,
    post_load,
    tcp_upstream,
    uds_upstream,
)
from _helpers import make_settings


def _renderer(settings):
    """Wrap settings in a minimal mock app and build a CaddyRenderer."""
    return CaddyRenderer(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


def _wd(settings):
    """Build a CaddyWatchdog from settings (wrapped in a minimal mock app)."""
    return CaddyWatchdog(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


# ---------------------------------------------------------------------------
# Fakes for the admin-API HTTP path (no real Caddy / no real socket)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad", request=httpx.Request("POST", "http://x"), response=self
            )


class _FakeAsyncClient:
    """A minimal stand-in for httpx.AsyncClient (post + get + async-cm)."""

    # class-level capture so tests can inspect the last POST without holding
    # a reference to the instance the SUT constructed.
    last_post: dict | None = None
    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.transport = kwargs.get("transport")
        self.closed = False
        self.posts: list[tuple] = []
        self.get_ok = kwargs.pop("get_ok", True)
        _FakeAsyncClient.instances.append(self)

    async def post(self, url, *, content=None, headers=None):
        self.posts.append((url, content, headers))
        _FakeAsyncClient.last_post = {
            "url": url,
            "content": content,
            "headers": headers,
        }
        return _FakeResponse()

    async def get(self, url):
        if not self.get_ok:
            raise httpx.ConnectError("no socket")
        return _FakeResponse()

    async def aclose(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeAsyncClient.last_post = None
    _FakeAsyncClient.instances.clear()
    yield
    _FakeAsyncClient.last_post = None
    _FakeAsyncClient.instances.clear()


# ---------------------------------------------------------------------------
# Upstream constructors
# ---------------------------------------------------------------------------


class TestUpstreams:
    def test_uds_upstream(self):
        # Caddy's UDS dial is ``unix//<path>``; an absolute path therefore
        # has three slashes (``unix//`` + ``/tmp/sock``).
        assert uds_upstream("/tmp/sock") == "unix///tmp/sock"
        assert uds_upstream("relative/sock") == "unix//relative/sock"

    def test_tcp_upstream(self):
        assert tcp_upstream("127.0.0.1", "8997") == "127.0.0.1:8997"


# ---------------------------------------------------------------------------
# post_load (admin API client)
# ---------------------------------------------------------------------------


class TestPostLoad:
    @pytest.mark.asyncio
    async def test_posts_caddyfile_with_text_content_type(self):
        """An injected client receives POST /load + text/caddyfile body."""
        client = _FakeAsyncClient()
        await post_load("/sock", "caddyfile body", client=client)
        assert _FakeAsyncClient.last_post["url"] == "http://localhost/load"
        assert _FakeAsyncClient.last_post["content"] == "caddyfile body"
        assert (
            _FakeAsyncClient.last_post["headers"]["Content-Type"]
            == CADDYFILE_CONTENT_TYPE
        )

    @pytest.mark.asyncio
    async def test_injected_client_not_closed(self):
        """An injected client is owned by the caller — post_load must not close it."""
        client = _FakeAsyncClient()
        await post_load("/sock", "x", client=client)
        assert client.closed is False

    @pytest.mark.asyncio
    async def test_own_client_constructed_with_uds_transport_and_closed(
        self, monkeypatch
    ):
        """The production path builds a UDS-backed client and closes it."""
        import klangk.caddy as caddy_mod

        transports: list[str] = []
        monkeypatch.setattr(
            caddy_mod.httpx,
            "AsyncHTTPTransport",
            lambda uds: transports.append(uds) or f"transport:{uds}",
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _FakeAsyncClient)
        await post_load("/the/sock", "x")
        assert transports == ["/the/sock"]
        assert (
            _FakeAsyncClient.instances
            and _FakeAsyncClient.instances[-1].closed
        )
        assert (
            _FakeAsyncClient.instances[-1].transport == "transport:/the/sock"
        )

    @pytest.mark.asyncio
    async def test_raises_on_error_status(self):
        """A 4xx/5xx from /load propagates as an httpx error."""
        client = _FakeAsyncClient()

        async def post(url, *, content=None, headers=None):
            return _FakeResponse(400)

        client.post = post
        with pytest.raises(httpx.HTTPStatusError):
            await post_load("/sock", "x", client=client)


# ---------------------------------------------------------------------------
# CaddyRenderer — shared computation
# ---------------------------------------------------------------------------


class TestMaxBodySize:
    def test_default_500mb(self):
        assert _renderer(make_settings({}))._max_body_size() == "500MB"

    def test_custom(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "10485760"})
        assert _renderer(s)._max_body_size() == "10MB"

    def test_minimum_1mb(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "100"})
        assert _renderer(s)._max_body_size() == "1MB"

    def test_garbage_falls_back(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "not-a-number"})
        assert _renderer(s)._max_body_size() == "500MB"


class TestContainerSourceLists:
    def test_egress_list_includes_all_sources(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        lst = _renderer(s)._egress_remote_ip_list()
        assert "10.89.0.0/24" in lst
        # loopback included in the egress allow set
        assert "127.0.0.1" in lst

    def test_browser_deny_list_excludes_loopback(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        lst = _renderer(s)._browser_deny_remote_ip_list()
        assert "10.89.0.0/24" in lst
        assert "127.0.0.1" not in lst

    def test_all_loopback_warns(self, caplog):
        s = make_settings({"KLANGK_CONTAINER_SUBNETS": "127.0.0.1"})
        with caplog.at_level("WARNING"):
            _renderer(s)._container_source_entries()
        assert "no non-loopback" in caplog.text

    def test_fallback_rfc1918_when_detection_empty(self, monkeypatch):
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(caddy_mod, "detect_host_ipv4s", lambda: [])
        s = make_settings({})
        lst = _renderer(s)._browser_deny_remote_ip_list()
        assert "172.16.0.0/12" in lst
        assert "10.0.0.0/8" in lst


# ---------------------------------------------------------------------------
# CaddyRenderer — section builders
# ---------------------------------------------------------------------------


class TestGlobalBlock:
    def test_admin_uds_autohttps_persist(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        g = _renderer(s)._global_block("/d/caddy-admin.sock")
        assert "admin unix///d/caddy-admin.sock|0600" in g
        assert "auto_https off" in g
        assert "persist_config off" in g

    def test_admin_uds_mode_suffix_is_0600(self):
        """The admin socket is created owner-only (#1559 locked decision: 0600,
        not Caddy's group-readable default). Regression for the perms review
        finding."""
        s = make_settings({"KLANGK_PORT": "8997"})
        g = _renderer(s)._global_block("/d/caddy-admin.sock")
        assert "|0600" in g
        assert "|0660" not in g
        assert "|0644" not in g

    def test_bootstrap_block_is_admin_only(self):
        """The initial --config carries only the admin global option, so Caddy
        binds the admin UDS at bootstrap on any version — not /dev/null (which
        falls back to localhost:2019 on Caddy < 2.7, #1709). Site config arrives
        later via POST /load, so auto_https / persist_config / trusted_proxies
        are deliberately absent here."""
        b = _renderer(make_settings({"KLANGK_PORT": "8997"}))._bootstrap_block(
            "/d/caddy-admin.sock"
        )
        # Establishes the admin endpoint at mode 0600.
        assert "admin unix///d/caddy-admin.sock|0600" in b
        assert b.startswith("{\n") and b.rstrip().endswith("}")
        # Site/global knobs are absent — they come via /load, not the bootstrap.
        assert "auto_https" not in b
        assert "persist_config" not in b
        assert "trusted_proxies" not in b
        assert "reverse_proxy" not in b

    def test_trusted_proxies_present_by_default(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,127.0.0.1",
            }
        )
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies static 10.0.0.0/8 127.0.0.1" in g
        assert "trusted_proxies_strict" in g

    def test_trusted_proxies_suppressed_when_reject(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_REJECT_PROXY_HEADERS": "1"}
        )
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies" not in g

    def test_trusted_proxies_defaults_to_loopback(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies static 127.0.0.1 ::1" in g

    def test_trusted_proxies_empty_falls_back_to_loopback(self):
        """All-empty/commas KLANGK_TRUSTED_PROXY_CIDRS → loopback fallback."""
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_TRUSTED_PROXY_CIDRS": ",,"}
        )
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies static 127.0.0.1 ::1" in g


class TestCommonHeaders:
    def test_has_host_and_real_ip_only(self):
        """Only Host + X-Real-IP (Caddy defaults cover X-Forwarded-*)."""
        h = _renderer(make_settings({}))._common_rp_headers()
        assert "header_up Host {host}" in h
        assert "header_up X-Real-IP {client_ip}" in h
        assert "X-Forwarded-For" not in h


class TestLlmBlock:
    def test_empty_without_url(self):
        assert (
            _renderer(make_settings({}))._build_llm_block(
                "upstream", "1.2.3.4"
            )
            == ""
        )

    def test_with_url_and_key(self):
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "sekret",
            }
        )
        guard = "\t\trespond @notContainerSrc 403\n"
        b = _renderer(s)._build_llm_block("upstream", guard)
        assert "path /llm-proxy/*" in b
        assert "reverse_proxy http://127.0.0.1:11434" in b
        assert 'Authorization "Bearer sekret"' in b
        assert "respond @notContainerSrc 403" in b

    def test_path_bearing_url_split_into_upstream_and_rewrite(self):
        """Path-bearing ``llm_base_url`` values (z.ai, OpenRouter, most
        providers) must not be passed verbatim to ``reverse_proxy`` — caddy
        rejects upstream URLs with a path component (``URLs for proxy
        upstreams only support scheme, host, and port components``), which
        crashed the whole LLM block for any non-host-root base URL. Split
        the host off into ``reverse_proxy`` and re-attach the path via
        ``rewrite`` so the final upstream path is preserved (#1681)."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "https://api.z.ai/api/coding/paas/v4",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        b = _renderer(s)._build_llm_block("upstream", "")
        # host-only upstream — no path component
        assert "reverse_proxy https://api.z.ai" in b
        assert "reverse_proxy https://api.z.ai/api/coding/paas/v4" not in b
        # handle_path strips /llm-proxy atomically before the rewrite reads
        # the URI; the rewrite then prepends the base path and uses
        # {http.request.uri.path} (path only — drops the container user's
        # query per the trust-boundary rule, #1687) so the final upstream
        # path is /api/coding/paas/v4/chat/completions.
        assert "handle_path /llm-proxy/*" in b
        assert "rewrite * /api/coding/paas/v4{http.request.uri.path}?" in b

    def test_path_bearing_url_trailing_slash_not_doubled(self):
        """A trailing slash on the base path must not double up: base_path
        "/v4/" stripped to "/v4" so concatenation with
        {http.request.uri.path} "/chat" yields "/v4/chat" rather than
        "/v4//chat"."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://proxy.local/v1/",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        b = _renderer(s)._build_llm_block("upstream", "")
        assert "rewrite * /v1{http.request.uri.path}?" in b
        assert "//{http.request.uri.path}" not in b

    def test_host_only_url_emits_path_only_rewrite(self):
        """A bare host (no path, no query) still emits a rewrite that uses
        {http.request.uri.path} — handle_path's strip alone would forward
        {uri} (path + query), but the container user's query is untrusted
        and must be dropped (#1687). The rewrite target is just the path
        with no prefix and no query."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        b = _renderer(s)._build_llm_block("upstream", "")
        assert "handle_path /llm-proxy/*" in b
        # Trailing "?" with empty base query — REQUIRED so caddy treats the
        # rewrite as query-replacing rather than query-preserving (otherwise
        # the container user's per-request query would leak to the upstream;
        # see review of #1696).
        assert "rewrite * {http.request.uri.path}?" in b
        assert "rewrite * {http.request.uri.path}\n" not in b

    def test_base_query_reattached_after_path(self):
        """A base URL with a query string (Gemini-style ?key=..., documented
        but discouraged by Google on security grounds; the OpenAI Python
        client also preserves hardcoded query params on base_url —
        openai/openai-python@73ea2f7) is preserved: the rewrite target is
        <base_path>{http.request.uri.path}?<base_query>. The container
        user's per-request query is dropped ({http.request.uri.path} is
        path-only), so only operator-configured query params reach the
        upstream (#1687)."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": (
                    "https://generativelanguage.googleapis.com/v1beta"
                    "?key=AIzaSy-example"
                ),
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        b = _renderer(s)._build_llm_block("upstream", "")
        assert (
            "rewrite * /v1beta{http.request.uri.path}?key=AIzaSy-example" in b
        )

    def test_base_query_with_no_path_reattached(self):
        """Base query with no base path: rewrite target is
        {http.request.uri.path}?<base_query> (no path prefix)."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://gateway.local?token=op-secret",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        b = _renderer(s)._build_llm_block("upstream", "")
        assert "rewrite * {http.request.uri.path}?token=op-secret" in b


class TestHostedBlock:
    def test_disabled_when_zero(self):
        s = make_settings({"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "0"})
        b = _renderer(s)._build_hosted_block()
        assert "respond 404" in b
        assert "reverse_proxy 127.0.0.1" not in b

    def test_enabled_emits_redirect_and_proxy(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        b = _renderer(s)._build_hosted_block()
        assert "path_regexp hostedsl" in b
        assert "redir {uri}/ 308" in b
        assert "path_regexp hosted" in b
        assert "reverse_proxy 127.0.0.1:{re.hosted.1}" in b


# ---------------------------------------------------------------------------
# CaddyRenderer — render_config compiles via `caddy adapt` (smoke)
# ---------------------------------------------------------------------------


class TestLlmBlockCaddyAdapt:
    """Smoke-test the rendered Caddyfile by adapting it through a real caddy.

    The string assertions in ``TestLlmBlock`` are brittle: a future edit that
    emits valid-looking-but-broken Caddyfile for the path-bearing case would
    pass them. This class compiles the rendered config with ``caddy adapt``
    and (for path-bearing URLs) inspects the adapted JSON to prove the
    rewrite runs after ``handle_path``'s prefix strip — i.e. the final
    upstream path is the intended one.

    CI's plain-pip unit job has no ``caddy`` binary, so every test skips
    when ``caddy`` is absent (the runtime e2e suite covers it under devenv
    where caddy is present). Locally with devenv, these run.
    """

    @staticmethod
    def _has_caddy() -> bool:
        import shutil

        return shutil.which("caddy") is not None

    @pytest.fixture(autouse=True)
    def _skip_without_caddy(self):
        if not self._has_caddy():
            pytest.skip("no `caddy` binary on PATH (run under devenv)")

    @staticmethod
    def _adapt(cf: str) -> dict:
        """Run `caddy adapt --adapter caddyfile` on a rendered config; return JSON."""
        import json
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".Caddyfile", delete=False) as f:
            f.write(cf)
            path = f.name
        try:
            r = subprocess.run(
                ["caddy", "adapt", "--config", path, "--adapter", "caddyfile"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            os.unlink(path)
        assert r.returncode == 0, f"caddy adapt failed:\n{r.stderr}"
        return json.loads(r.stdout)

    @staticmethod
    def _find_llm_subroute(adapted: dict) -> list:
        """Walk the adapted config; return the routes inside the /llm-proxy handle_path block."""
        routes_out = []
        for server in adapted.get("apps", {}).get("http", {}).get("servers", {}).values():
            for route in server.get("routes", []):
                match = route.get("match", [])
                if not any("/llm-proxy" in str(m) for m in match):
                    continue
                # The match-all handle wraps a subroute; descend into it.
                for h in route.get("handle", []):
                    for sub in h.get("routes", []):
                        routes_out.append(sub)
        return routes_out

    def test_path_bearing_url_compiles_and_routes_correctly(self):
        """End-to-end render + caddy adapt for a path-bearing llm_base_url.

        Asserts the property the substring tests can't: the rewrite that
        re-attaches the base path runs AFTER handle_path's strip_prefix,
        so a request to /llm-proxy/chat/completions lands at
        /api/coding/paas/v4/chat/completions on the upstream. This is the
        headline fix of #1681; without a compile-level test a future edit
        that broke the strip+rewrite ordering (e.g. by switching back to
        ``uri strip_prefix`` + ``rewrite``, which caddy reorders) would
        silently ship broken.
        """
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "https://api.z.ai/api/coding/paas/v4",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        cf = _renderer(s).render_config("unix//s", "/d/caddy-admin.sock")
        adapted = self._adapt(cf)

        routes = self._find_llm_subroute(adapted)
        assert routes, "no /llm-proxy route in adapted config"

        # The strip_path_prefix handler must come before the rewrite handler.
        handlers = []
        for r in routes:
            for h in r.get("handle", []):
                if "strip_path_prefix" in h:
                    handlers.append("strip")
                elif "uri" in h and "rewrite" == h.get("handler"):
                    handlers.append("rewrite")
        assert "strip" in handlers, "handle_path's strip_prefix missing"
        assert "rewrite" in handlers, "base-path rewrite missing"
        assert handlers.index("strip") < handlers.index("rewrite"), (
            "strip must run before rewrite; otherwise the rewrite sees the "
            "un-stripped {uri} and the upstream path is wrong (regression of #1680)"
        )

        # And the rewrite target is the base path + {uri} (so a /chat request
        # lands at /api/coding/paas/v4/chat).
        rewrite_uris = [
            h["uri"]
            for r in routes
            for h in r.get("handle", [])
            if h.get("handler") == "rewrite" and "uri" in h
        ]
        assert any(
            "/api/coding/paas/v4" in u for u in rewrite_uris
        ), f"base path not in rewrite target: {rewrite_uris}"

    def test_host_only_url_compiles(self):
        """Sanity: a host-only URL (no path) still compiles — regression guard
        that the path-split refactor didn't break the simple case."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        cf = _renderer(s).render_config("unix//s", "/d/caddy-admin.sock")
        # Smoke: just confirm caddy accepts it. No path assertions needed.
        self._adapt(cf)

    def test_base_query_url_compiles(self):
        """A base URL with a query string (Gemini-style ?key=...) compiles
        — the rewrite target ``<path>{http.request.uri.path}?<query>`` is
        valid Caddyfile syntax. Regression guard that the #1687 query-
        preserve work didn't introduce a Caddyfile syntax error for the
        query case."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": (
                    "https://generativelanguage.googleapis.com/v1beta"
                    "?key=AIzaSy-example"
                ),
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        cf = _renderer(s).render_config("unix//s", "/d/caddy-admin.sock")
        adapted = self._adapt(cf)
        # And the rewrite target carries the base query.
        routes = self._find_llm_subroute(adapted)
        rewrite_uris = [
            h["uri"]
            for r in routes
            for h in r.get("handle", [])
            if h.get("handler") == "rewrite" and "uri" in h
        ]
        assert any(
            "?key=AIzaSy-example" in u for u in rewrite_uris
        ), f"base query not in rewrite target: {rewrite_uris}"


# ---------------------------------------------------------------------------
# CaddyRenderer — behavioral query-drop test (real caddy + echo upstream)
# ---------------------------------------------------------------------------


class TestLlmBlockCaddyQueryDrop:
    """Behavioral test for the #1687 trust-boundary claim.

    The substring tests above assert the rewrite *shape*; this class asserts
    the actual security property by running a real ``caddy`` against a real
    echo upstream and sending a request with a user-supplied query. If the
    rewrite ever stops forcing an empty/explicit query component, the
    upstream will see the user's query — the test catches it. (Substring
    tests would not — they lock in the rendered text, not the behavior.)

    CI's plain-pip unit job has no ``caddy``; the autouse fixture skips.
    """

    @staticmethod
    def _has_caddy() -> bool:
        import shutil

        return shutil.which("caddy") is not None

    @pytest.fixture(autouse=True)
    def _skip_without_caddy(self):
        if not self._has_caddy():
            pytest.skip("no `caddy` binary on PATH (run under devenv)")

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _start_echo(port: int):
        """Minimal HTTP echo: returns the request path (incl. query) as body."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                body = self.path.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", port), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv

    @staticmethod
    def _stop_echo(srv) -> None:
        srv.shutdown()
        srv.server_close()

    @staticmethod
    def _start_caddy(cf_path: str):
        import subprocess

        proc = subprocess.Popen(
            ["caddy", "run", "--config", cf_path, "--adapter", "caddyfile"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc

    @staticmethod
    def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
        import socket
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket() as s:
                s.settimeout(0.25)
                try:
                    s.connect((host, port))
                    return True
                except OSError:
                    time.sleep(0.1)
        return False

    @pytest.mark.parametrize(
        "base_url",
        [
            # Common case: host-only, no path, no query (local Ollama shape).
            # The trust-boundary property must hold here too — this is the
            # exact case the original PR #1696 silently leaked.
            "http://127.0.0.1:{echo_port}",
            # Path-bearing, no query (z.ai shape).
            "http://127.0.0.1:{echo_port}/v1beta",
        ],
    )
    def test_user_query_dropped_at_upstream(self, base_url, tmp_path):
        """A container user's per-request query MUST NOT reach the upstream.

        The base URL is operator config; the per-request query is
        untrusted. Sending ``/llm-proxy/chat?user_supplied=evil`` must
        produce ``/chat`` (or ``/<base_path>/chat``) at the upstream, with
        NO ``?user_supplied=evil``. Caddy's rewrite preserves the incoming
        query by default when the rewrite target has no query component —
        the renderer must always emit a query component (even empty) to
        force query-replacement. (#1696 review.)
        """
        import os
        import time
        import subprocess
        import urllib.request

        echo_port = self._free_port()
        proxy_port = self._free_port()
        url = base_url.format(echo_port=echo_port)

        srv = self._start_echo(echo_port)
        # Minimal Caddyfile mirroring _build_llm_block's shape for a
        # no-base-query URL: handle_path + rewrite with a forced empty query.
        # We render it by hand here rather than calling _build_llm_block so
        # the test pins the runtime contract independently of the renderer's
        # exact string (a renderer change that breaks this is caught by the
        # unit/compile tests above; this test catches the *property*).
        from klangk.caddy import CaddyRenderer
        import types
        from _helpers import make_settings

        s = make_settings(
            env={"KLANGK_LLM_BASE_URL": url, "KLANGK_LLM_API_KEY": "k"}
        )
        block = CaddyRenderer(
            types.SimpleNamespace(state=types.SimpleNamespace(settings=s))
        )._build_llm_block("upstream", "")
        # Strip the Authorization header line before writing to disk — the
        # api_key ("k") is not a real secret, but the rendered Caddyfile
        # would contain ``Authorization "Bearer k"`` and CodeQL flags any
        # write of api_key-derived data as clear-text storage of sensitive
        # information. The trust-boundary property under test (rewrite
        # drops the user's query) is independent of the Authorization
        # header, so stripping the line doesn't weaken the test.
        block_sanitized = "\n".join(
            line
            for line in block.splitlines()
            if not line.strip().startswith("header_up Authorization")
        )
        cf = (
            "{\n\tadmin off\n}\n"
            f":{proxy_port} {{\n{block_sanitized}\n}}\n"
        )
        cf_path = str(tmp_path / "Caddyfile")
        with open(cf_path, "w") as f:
            f.write(cf)

        proc = self._start_caddy(cf_path)
        try:
            assert self._wait_for_port("127.0.0.1", proxy_port), (
                "caddy did not start"
            )
            # POST /llm-proxy/chat?user_supplied=evil — upstream must NOT
            # see ?user_supplied=evil.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{proxy_port}/llm-proxy/chat?user_supplied=evil",
                timeout=5,
            ) as r:
                upstream_saw = r.read().decode()
            assert "user_supplied=evil" not in upstream_saw, (
                f"caddy leaked the container user's query to the upstream "
                f"(upstream saw {upstream_saw!r}). The rewrite target must "
                f"force an empty/explicit query component so caddy treats "
                f"it as query-replacing rather than query-preserving."
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._stop_echo(srv)


# ---------------------------------------------------------------------------
# CaddyRenderer — render_config (structure)
# ---------------------------------------------------------------------------


class TestRenderConfig:
    ADMIN = "/d/caddy-admin.sock"

    def test_full_has_two_listeners(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config(
            tcp_upstream("127.0.0.1", "8997"), self.ADMIN
        )
        # browser + egress site blocks
        assert "http://:8997 {" in cf
        assert "http://:8995 {" in cf
        assert "bind 127.0.0.1" in cf
        assert "bind 0.0.0.0" in cf

    def test_headless_has_only_egress(self):
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//sock", self.ADMIN)
        assert "http://:8995 {" in cf
        assert "http://:8997" not in cf
        assert "bind 0.0.0.0" in cf

    def test_template_keys_off_port_not_auth(self):
        for auth in ("none", "password", "both"):
            sh = make_settings(
                env={"KLANGK_AUTH_MODES": auth, "KLANGK_EGRESS_PORT": "8995"}
            )
            assert "http://:8997" not in _renderer(sh).render_config(
                "unix//s", self.ADMIN
            )
            sf = make_settings(
                env={
                    "KLANGK_AUTH_MODES": auth,
                    "KLANGK_PORT": "8997",
                    "KLANGK_EGRESS_PORT": "8995",
                }
            )
            assert "http://:8997 {" in _renderer(sf).render_config(
                "unix//s", self.ADMIN
            )

    def test_forward_auth_present(self):
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "forward_auth unix//s {" in cf
        assert "uri /api/v1/auth/verify-workspace-token" in cf

    def test_request_body_max_size(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_FILE_UPLOAD_SIZE_MAX": "10485760"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "max_size 10MB" in cf

    def test_auth_local_loopback_acl(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # nginx uses ``location =`` (exact); Caddy mirrors with a path matcher.
        assert "@authlocal path /api/v1/auth/local" in cf
        assert "handle @authlocal {" in cf
        assert "@notLoopback not remote_ip 127.0.0.1 ::1" in cf
        assert "respond @notLoopback 403" in cf

    def test_auth_local_is_exact_match(self):
        """/auth/local uses an exact path matcher (nginx ``location =``), so a
        sub-path like /api/v1/auth/local/other does NOT match the handle."""
        s = make_settings({"KLANGK_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "path /api/v1/auth/local\n" in cf
        # no trailing /* (which would make it prefix)
        assert "path /api/v1/auth/local/*" not in cf

    def test_browser_catch_all_container_deny(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@containerSrc remote_ip 10.89.0.0/24" in cf
        assert "respond @containerSrc 403" in cf

    def test_browser_deny_uses_immediate_peer_matcher(self):
        """Regression guard (#1546): the container-source *deny matcher* keys
        on ``remote_ip`` (immediate peer, ignores trusted_proxies), never
        ``client_ip`` (which would re-introduce the #1546 403). ``{client_ip}``
        as an ``X-Real-IP`` header_up value is unrelated and fine."""
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@containerSrc remote_ip 10.89.0.0/24" in cf
        # No container-source deny keyed on client_ip anywhere.
        assert "@containerSrc client_ip" not in cf
        assert "not client_ip" not in cf

    def test_egress_acl_uses_remote_ip(self):
        s = make_settings(
            env={
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@notContainerSrc not remote_ip 10.89.0.0/24" in cf

    def test_post_chat_message_is_exact_match(self):
        """nginx uses ``location =`` (exact) for post-chat-message; Caddy
        mirrors with a path matcher so sub-paths don't match."""
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@postchat path /api/v1/workspaces/post-chat-message" in cf
        assert "handle @postchat {" in cf
        assert "path /api/v1/workspaces/post-chat-message/*" not in cf

    def test_egress_fail_closed_when_no_container_sources(self, monkeypatch):
        """Whitespace-only KLANGK_CONTAINER_SUBNETS → no sources → egress
        fails closed (deny all), matching nginx's bare ``deny all;``."""
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(caddy_mod, "detect_host_ipv4s", lambda: [])
        s = make_settings(
            env={
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_CONTAINER_SUBNETS": "   ",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # No @notContainerSrc matcher is defined ...
        assert "@notContainerSrc" not in cf
        # ... but the egress locations still deny (bare respond 403).
        assert "respond 403" in cf

    def test_browser_no_deny_when_all_loopback(self):
        """All-loopback container sources → no non-loopback deny set → the
        browser catch-all emits no guard (loopback + remotes all pass), the
        nginx ``geo default 0`` equivalent. Regression for the empty-set case
        that previously left a dangling @containerSrc reference."""
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_CONTAINER_SUBNETS": "127.0.0.1"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@containerSrc" not in cf
        assert "respond @containerSrc 403" not in cf
        # The catch-all still proxies.
        assert "handle {" in cf

    def test_llm_api_key_resolved(self):
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "cmd:printf %s resolved-key",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert 'Authorization "Bearer resolved-key"' in cf
        assert "cmd:" not in cf

    def test_llm_proxy_strips_prefix(self):
        """The /llm-proxy/ prefix is stripped before proxying — nginx rewrites
        ``/llm-proxy/(.*)`` → ``{base_url}/$1``; Caddy mirrors with
        ``handle_path /llm-proxy/*``, which atomically matches the path AND
        strips the prefix before any subsequent directive in the same block
        reads {uri}. (A plain ``uri strip_prefix`` followed by ``rewrite``
        does NOT work — caddy's adapter reorders the two rewrite-family
        handlers, so the rewrite sees the un-stripped {uri}.) Without the
        strip, the path forwards verbatim and 404s at every provider
        (regression, found in review)."""
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "handle_path /llm-proxy/*" in cf

    def test_llm_absent_without_url(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "/llm-proxy" not in cf

    def test_global_block_prepended(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # global block is first, before any site block.
        admin_pos = cf.index("admin unix//")
        first_site = cf.index("http://")
        assert admin_pos < first_site


class TestFindProxyBin:
    def test_configured(self):
        s = make_settings({"KLANGK_PROXY_BIN": "/custom/caddy"})
        assert _renderer(s).find_proxy_bin() == "/custom/caddy"

    def test_fallback_to_which(self, monkeypatch):
        """When shutil.which finds caddy, that path is used (not the hard-coded
        fallback). Forced via monkeypatch so it's deterministic on hosts
        without caddy on PATH (e.g. CI's plain-pip unit job)."""
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(
            caddy_mod.shutil, "which", lambda name: "/found/caddy"
        )
        assert _renderer(make_settings({})).find_proxy_bin() == "/found/caddy"

    def test_fallback_to_usr_bin(self, monkeypatch):
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(caddy_mod.shutil, "which", lambda _: None)
        assert (
            _renderer(make_settings({})).find_proxy_bin() == "/usr/bin/caddy"
        )


# ---------------------------------------------------------------------------
# CaddyWatchdog
# ---------------------------------------------------------------------------


class TestWatchdogPaths:
    def test_admin_socket_under_state_dir(self, tmp_path):
        s = make_settings({"KLANGK_STATE_DIR": str(tmp_path)})
        wd = _wd(s)
        assert wd.admin_socket == str(tmp_path / "caddy-admin.sock")

    def test_admin_socket_override(self, tmp_path):
        """KLANGK_CADDY_ADMIN_SOCKET overrides the default path (#1636) — read
        live off settings, not built inline."""
        s = make_settings(
            {
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_CADDY_ADMIN_SOCKET": "/short/caddy-admin.sock",
            }
        )
        wd = _wd(s)
        assert wd.admin_socket == "/short/caddy-admin.sock"
        assert wd.admin_bind_address == "unix///short/caddy-admin.sock|0600"

    def test_admin_bind_address_has_0600_suffix(self, tmp_path):
        """The Caddy bind address carries |0600 (owner-only socket creation);
        the bare path (admin_socket) is what httpx dials."""
        s = make_settings({"KLANGK_STATE_DIR": str(tmp_path)})
        wd = _wd(s)
        assert (
            wd.admin_bind_address == f"unix//{tmp_path}/caddy-admin.sock|0600"
        )
        assert wd.admin_socket == str(tmp_path / "caddy-admin.sock")

    def test_find_proxy_bin_delegates_to_renderer(self, monkeypatch):
        s = make_settings({"KLANGK_PROXY_BIN": "/x/caddy"})
        assert _wd(s).find_proxy_bin() == "/x/caddy"


class TestWatchdogLoadConfig:
    @pytest.mark.asyncio
    async def test_renders_and_posts(self, monkeypatch):
        """load_config renders the Caddyfile (UDS upstream) and POSTs it."""
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        wd = _wd(s)
        client = _FakeAsyncClient()
        await wd.load_config(client=client)
        assert _FakeAsyncClient.last_post is not None
        body = _FakeAsyncClient.last_post["content"]
        assert "auto_https off" in body
        assert (
            _FakeAsyncClient.last_post["headers"]["Content-Type"]
            == CADDYFILE_CONTENT_TYPE
        )

    @pytest.mark.asyncio
    async def test_explicit_caddyfile_passed_through(self):
        s = make_settings({})
        wd = _wd(s)
        client = _FakeAsyncClient()
        await wd.load_config("my caddyfile", client=client)
        assert _FakeAsyncClient.last_post["content"] == "my caddyfile"


class TestWaitForAdmin:
    @pytest.mark.asyncio
    async def test_returns_true_when_reachable(self, monkeypatch):
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(
            caddy_mod.httpx, "AsyncHTTPTransport", lambda uds: None
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _FakeAsyncClient)
        s = make_settings({})
        wd = _wd(s)
        assert await wd._wait_for_admin(timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_returns_true_on_any_response_status(self, monkeypatch):
        """Any HTTP response (even an error status) counts as admin-up —
        only connection failure retries."""
        import klangk.caddy as caddy_mod

        class _UpButError(_FakeAsyncClient):
            async def get(self, url):
                return _FakeResponse(500)

        monkeypatch.setattr(
            caddy_mod.httpx, "AsyncHTTPTransport", lambda uds: None
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _UpButError)
        wd = _wd(make_settings({}))
        assert await wd._wait_for_admin(timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_never_reachable(self, monkeypatch):
        """Connection failure on every poll → sleep + retry → False at timeout."""
        import klangk.caddy as caddy_mod

        slept = []

        async def _fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(caddy_mod.asyncio, "sleep", _fake_sleep)

        class _NeverUp(_FakeAsyncClient):
            async def get(self, url):
                raise httpx.ConnectError("no socket")

        monkeypatch.setattr(
            caddy_mod.httpx, "AsyncHTTPTransport", lambda uds: None
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _NeverUp)
        wd = _wd(make_settings({}))
        # Small timeout → a couple of 0.2s polls then give up.
        assert await wd._wait_for_admin(timeout=0.001) is False
        assert slept  # the retry path slept at least once


class TestWatchdogStart:
    @pytest.mark.asyncio
    async def test_start_noop_when_disabled(self, monkeypatch):
        monkeypatch.setenv("_KLANGK_DISABLE_PROXY", "1")
        wd = _wd(make_settings({}))
        await wd.start()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_start_runs_prepare_and_spawns(self, monkeypatch, tmp_path):
        """When enabled, start() resolves the bin + schedules the watchdog."""
        s = make_settings(
            env={
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_SOCKET": str(tmp_path / "klangk.sock"),
                "KLANGK_EGRESS_PORT": "19999",
            }
        )
        monkeypatch.delenv("_KLANGK_DISABLE_PROXY", raising=False)
        monkeypatch.setattr(
            "klangk.caddy.CaddyRenderer.find_proxy_bin",
            lambda self: "/fake/caddy",
        )

        spawned = {}

        async def _fake_watch(self_wd, bin_path):
            spawned["bin"] = bin_path

        monkeypatch.setattr(CaddyWatchdog, "_watch", _fake_watch)
        wd = _wd(s)
        await wd.start()
        try:
            assert wd._task is not None
            assert wd._stopping is False
            await wd._task
            assert spawned["bin"] == "/fake/caddy"
        finally:
            pass


class TestWatchdogStop:
    @pytest.mark.asyncio
    async def test_stops_no_proc_no_task(self):
        wd = _wd(make_settings({}))
        await wd.stop()
        assert wd._proc is None
        assert wd._task is None
        assert wd._stopping is True

    @pytest.mark.asyncio
    async def test_stops_terminates_running_proc(self, monkeypatch):
        killpg_calls = []
        monkeypatch.setattr(
            "os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
        )

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert killpg_calls == [(12345, signal.SIGTERM)]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_falls_back_to_terminate(self, monkeypatch):
        monkeypatch.setattr("os.killpg", Mock(side_effect=ProcessLookupError))
        terminated = []

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                terminated.append(True)

            def kill(self):
                pass

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert terminated == [True]

    @pytest.mark.asyncio
    async def test_stops_cancels_task(self):
        async def _long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        wd = _wd(make_settings({}))
        wd._task = asyncio.create_task(_long())
        await wd.stop()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_stops_kills_on_timeout(self, monkeypatch):
        import klangk.caddy as caddy_mod

        actions = []
        monkeypatch.setattr(
            "os.killpg", lambda pgid, sig: actions.append(("killpg", sig))
        )

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                actions.append("terminate")

            def kill(self):
                actions.append("kill")

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(caddy_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [
            ("killpg", signal.SIGTERM),
            ("killpg", signal.SIGKILL),
        ]

    @pytest.mark.asyncio
    async def test_stops_kills_fallback_on_timeout(self, monkeypatch):
        import klangk.caddy as caddy_mod

        actions = []
        calls = [0]

        def fake_killpg(pgid, sig):
            calls[0] += 1
            if calls[0] == 1:
                actions.append(("killpg", sig))
            else:
                raise ProcessLookupError

        monkeypatch.setattr("os.killpg", fake_killpg)

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                actions.append("kill")

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(caddy_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [("killpg", signal.SIGTERM), "kill"]


class TestWatchdogReconfigure:
    def test_reconfigure_swaps_app_and_flags_reload(self):
        wd = _wd(make_settings({}))
        new_app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wd.reconfigure(new_app)
        assert wd.app is new_app
        assert wd._renderer.app is new_app
        # reconfigure flags a pending admin-API reload (#1559: a settings
        # change is a fresh POST /load).
        assert wd._pending_reload is True

    @pytest.mark.asyncio
    async def test_apply_pending_reload_noop_when_not_flagged(self):
        """No reload flag → apply is a no-op (no load_config call)."""
        wd = _wd(make_settings({}))
        wd._task = object()  # pretend started so we reach the load guard
        called = []
        wd.load_config = AsyncMock(side_effect=lambda: called.append(1))
        await wd.apply_pending_reload()
        assert called == []

    @pytest.mark.asyncio
    async def test_apply_pending_reload_noop_when_not_started(self):
        """Flag set but watchdog never started (disabled) → no load attempt."""
        wd = _wd(make_settings({}))
        wd.reconfigure(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        assert wd._task is None  # never started
        called = []
        wd.load_config = AsyncMock(side_effect=lambda: called.append(1))
        await wd.apply_pending_reload()
        assert called == []
        assert wd._pending_reload is False  # flag cleared

    @pytest.mark.asyncio
    async def test_apply_pending_reload_pushes_when_running(self):
        """Flagged + started → load_config is called and the flag clears."""
        wd = _wd(make_settings({}))
        wd._task = object()  # started
        wd.reconfigure(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        wd.load_config = AsyncMock()
        await wd.apply_pending_reload()
        wd.load_config.assert_awaited_once()
        assert wd._pending_reload is False

    @pytest.mark.asyncio
    async def test_apply_pending_reload_swallows_load_failure(self):
        """A load_config failure is logged + swallowed (Caddy keeps its
        last-known-good config); the flag still clears so we don't retry-loop."""
        wd = _wd(make_settings({}))
        wd._task = object()
        wd.reconfigure(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        wd.load_config = AsyncMock(side_effect=httpx.ConnectError("down"))
        await wd.apply_pending_reload()  # must not raise
        assert wd._pending_reload is False
