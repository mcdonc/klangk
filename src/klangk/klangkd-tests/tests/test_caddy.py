"""Unit tests for the Caddy proxy engine (#1559).

Parallel to ``test_proxy.py`` (the nginx engine). These exercise the pure
Caddyfile rendering logic + the admin-API client / watchdog orchestration
without a running Caddy — the runtime enforcement (spawn/respawn, ACLs,
forward_auth) is covered by the e2e suite (``test_caddy_*_e2e.py``, run under
devenv where the ``caddy`` binary is present; CI's plain-pip unit job has no
caddy, so nothing here shells out to it).
"""

import asyncio
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
    config_request,
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
    def __init__(self, status_code: int = 200, json_data=None) -> None:
        self.status_code = status_code
        self.text = ""
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad", request=httpx.Request("POST", "http://x"), response=self
            )


class _FakeAsyncClient:
    """A minimal stand-in for httpx.AsyncClient (post + get + request + async-cm)."""

    # class-level capture so tests can inspect the last POST without holding
    # a reference to the instance the SUT constructed.
    last_post: dict | None = None
    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.transport = kwargs.get("transport")
        self.closed = False
        self.posts: list[tuple] = []
        self.get_ok = kwargs.pop("get_ok", True)
        # Config-tree (request()) capture: per-instance list of
        # (method, url, json_body) plus a callable the test can set to
        # synthesize responses. Default: 200 with no body.
        self.requests: list[tuple] = []
        self.request_responder = kwargs.pop("request_responder", None)
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

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        if self.request_responder is not None:
            return self.request_responder(method, url, kwargs.get("json"))
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
        ``uri strip_prefix /llm-proxy``. Without it, the path forwards
        verbatim and 404s at every provider (regression, found in review)."""
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "uri strip_prefix /llm-proxy" in cf
        # the strip precedes the reverse_proxy in the handle block
        strip_pos = cf.index("uri strip_prefix /llm-proxy")
        rp_pos = cf.index("reverse_proxy http://127.0.0.1:11434")
        assert strip_pos < rp_pos

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


# ---------------------------------------------------------------------------
# config_request (path-based admin API client, #1633 Phase 3)
# ---------------------------------------------------------------------------


class TestConfigRequest:
    """The generic config-tree REST client (GET/POST/PUT/PATCH/DELETE)."""

    @pytest.mark.asyncio
    async def test_get_uses_request_no_body(self):
        client = _FakeAsyncClient()
        await config_request(
            "GET", "/sock", "/config/apps/http/servers", client=client
        )
        assert client.requests[-1][0] == "GET"
        assert (
            client.requests[-1][1]
            == "http://localhost/config/apps/http/servers"
        )
        assert client.requests[-1][2] is None  # no body on GET

    @pytest.mark.asyncio
    async def test_post_serializes_json_body(self):
        client = _FakeAsyncClient()
        route = {"@id": "x", "match": [{"path": ["/x"]}], "handle": []}
        await config_request(
            "POST",
            "/sock",
            "/config/apps/http/servers/srv0/routes",
            body=route,
            client=client,
        )
        method, url, body = client.requests[-1]
        assert method == "POST"
        assert url == "http://localhost/config/apps/http/servers/srv0/routes"
        # body is the dict, not pre-serialized — httpx does json= encoding.
        assert body == route

    @pytest.mark.asyncio
    async def test_delete_path(self):
        client = _FakeAsyncClient()
        await config_request("DELETE", "/sock", "/id/myroute", client=client)
        assert client.requests[-1][0] == "DELETE"
        assert client.requests[-1][1] == "http://localhost/id/myroute"
        assert client.requests[-1][2] is None

    @pytest.mark.asyncio
    async def test_raises_for_status(self):
        """A 4xx/5xx surfaces as httpx.HTTPStatusError."""
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(404)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await config_request("GET", "/sock", "/id/missing", client=client)

    @pytest.mark.asyncio
    async def test_owns_and_closes_client_when_none_injected(
        self, monkeypatch
    ):
        """client=None → builds a UDS-backed client and closes it after."""
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(
            caddy_mod.httpx,
            "AsyncHTTPTransport",
            lambda uds: "transport:/cfg/sock",
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _FakeAsyncClient)
        await config_request(
            "GET",
            "/cfg/sock",
            "/config/",
        )
        assert (
            _FakeAsyncClient.instances
            and _FakeAsyncClient.instances[-1].closed
        )


# ---------------------------------------------------------------------------
# CaddyWatchdog dynamic-route mutations (#1633 Phase 3)
# ---------------------------------------------------------------------------


def _servers_responder(servers_json):
    """A request_responder that returns the servers dict for GET .../servers,
    200 otherwise. Models the real admin API: one GET to read servers, then
    POST/DELETE/GET /id/... for the route ops."""

    def _r(method, url, body):
        if method == "GET" and url.endswith("/servers"):
            return _FakeResponse(200, json_data=servers_json)
        return _FakeResponse(200, json_data={"@id": "r"} if body else None)

    return _r


class TestServerKeyForPort:
    @pytest.mark.asyncio
    async def test_finds_server_by_listen_port(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {
                    "srv0": {"listen": ["0.0.0.0:8995"]},
                    "srv1": {"listen": ["127.0.0.1:19998"]},
                }
            )
        )
        assert await wd._server_key_for_port("8995", client=client) == "srv0"
        assert await wd._server_key_for_port("19998", client=client) == "srv1"

    @pytest.mark.asyncio
    async def test_returns_none_when_port_not_listening(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {"srv0": {"listen": ["0.0.0.0:8995"]}}
            )
        )
        assert await wd._server_key_for_port("9999", client=client) is None


class TestAddRoute:
    @pytest.mark.asyncio
    async def test_posts_to_resolved_server_routes_and_tracks(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {"srv0": {"listen": ["0.0.0.0:8995"]}}
            )
        )
        route = {
            "@id": "ws-123-hosted",
            "match": [{"path": ["/dynamic/*"]}],
            "handle": [{"handler": "static_response", "status_code": "200"}],
        }
        await wd.add_route("8995", route, client=client)
        # The POST targeted the resolved server key, with the route as body.
        posts = [r for r in client.requests if r[0] == "POST"]
        assert posts[-1][1] == (
            "http://localhost/config/apps/http/servers/srv0/routes"
        )
        assert posts[-1][2] == route
        # Tracked for re-apply after /load.
        assert wd._dynamic_routes["ws-123-hosted"] == ("8995", route)

    @pytest.mark.asyncio
    async def test_requires_at_id(self):
        wd = _wd(make_settings({}))
        with pytest.raises(ValueError, match="@id"):
            await wd.add_route(
                "8995",
                {"match": [{"path": ["/x"]}]},
                client=_FakeAsyncClient(),
            )

    @pytest.mark.asyncio
    async def test_raises_when_no_server_listens_on_port(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {"srv0": {"listen": ["0.0.0.0:8995"]}}
            )
        )
        with pytest.raises(RuntimeError, match="no caddy server"):
            await wd.add_route("9999", {"@id": "x"}, client=client)


class TestGetRoute:
    @pytest.mark.asyncio
    async def test_returns_route_json(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(
                200, json_data={"@id": "r", "match": [{"path": ["/x"]}]}
            )
        )
        assert (await wd.get_route("r", client=client)) == {
            "@id": "r",
            "match": [{"path": ["/x"]}],
        }

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(404)
        )
        assert await wd.get_route("missing", client=client) is None

    @pytest.mark.asyncio
    async def test_reraises_non_404_errors(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await wd.get_route("r", client=client)


class TestPutRoute:
    @pytest.mark.asyncio
    async def test_puts_by_id_and_updates_tracking(self):
        wd = _wd(make_settings({}))
        # Seed tracking so put_route reuses the recorded server port.
        wd._dynamic_routes["r"] = (
            "8995",
            {"@id": "r", "handle": [{"handler": "subroute"}]},
        )
        client = _FakeAsyncClient()
        new = {"@id": "r", "match": [{"path": ["/y"]}]}
        await wd.put_route("r", new, client=client)
        puts = [x for x in client.requests if x[0] == "PUT"]
        assert puts[-1][1] == "http://localhost/id/r"
        assert puts[-1][2] == new
        assert wd._dynamic_routes["r"] == ("8995", new)

    @pytest.mark.asyncio
    async def test_put_brand_new_id_falls_back_to_egress_port(self):
        """put_route on an id NOT already tracked records it against the
        egress port (the _find_port_for_route fallback — dynamic routes are
        an egress-side concern in practice). Covers the put_route else-branch."""
        wd = _wd(make_settings(env={"KLANGK_EGRESS_PORT": "8995"}))
        client = _FakeAsyncClient()
        new = {"@id": "fresh", "match": [{"path": ["/z"]}]}
        await wd.put_route("fresh", new, client=client)
        puts = [x for x in client.requests if x[0] == "PUT"]
        assert puts[-1][1] == "http://localhost/id/fresh"
        # Tracked against the egress port (the fallback), ready for re-apply.
        assert wd._dynamic_routes["fresh"] == ("8995", new)


class TestDeleteRoute:
    @pytest.mark.asyncio
    async def test_deletes_by_id_and_untracks(self):
        wd = _wd(make_settings({}))
        wd._dynamic_routes["r"] = ("8995", {"@id": "r"})
        client = _FakeAsyncClient()
        assert await wd.delete_route("r", client=client) is True
        dels = [x for x in client.requests if x[0] == "DELETE"]
        assert dels[-1][1] == "http://localhost/id/r"
        assert "r" not in wd._dynamic_routes

    @pytest.mark.asyncio
    async def test_returns_false_on_404_and_untracks(self):
        wd = _wd(make_settings({}))
        wd._dynamic_routes["ghost"] = ("8995", {"@id": "ghost"})
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(404)
        )
        assert await wd.delete_route("ghost", client=client) is False
        assert "ghost" not in wd._dynamic_routes

    @pytest.mark.asyncio
    async def test_reraises_non_404_errors(self):
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient(
            request_responder=lambda m, u, b: _FakeResponse(500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await wd.delete_route("r", client=client)


class TestReapplyDynamicRoutes:
    @pytest.mark.asyncio
    async def test_reposts_tracked_routes_after_load(self):
        """load_config re-POSTs tracked routes because /load wipes them."""
        wd = _wd(make_settings({}))
        # Two tracked routes on the egress server (8995).
        wd._dynamic_routes = {
            "a": ("8995", {"@id": "a", "match": [{"path": ["/a"]}]}),
            "b": ("8995", {"@id": "b", "match": [{"path": ["/b"]}]}),
        }
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {"srv0": {"listen": ["0.0.0.0:8995"]}}
            )
        )
        await wd.load_config("cf", client=client)
        # The full /load POST happened...
        assert _FakeAsyncClient.last_post["content"] == "cf"
        # ...then both dynamic routes were re-POSTed to the resolved server.
        posts = [r for r in client.requests if r[0] == "POST"]
        route_posts = [
            p
            for p in posts
            if p[1] == "http://localhost/config/apps/http/servers/srv0/routes"
        ]
        assert {p[2]["@id"] for p in route_posts} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_load_skips_reapply_when_no_tracked_routes(self):
        """No dynamic routes → load_config is just the POST /load (no extra
        requests). Keeps the existing Phase-1 behavior unchanged."""
        wd = _wd(make_settings({}))
        client = _FakeAsyncClient()
        await wd.load_config("cf", client=client)
        assert client.requests == []  # no config_request calls

    @pytest.mark.asyncio
    async def test_reapply_skips_missing_server(self):
        """A tracked route whose server is gone (e.g. headless after a mode
        swap) is skipped, not fatal — the other routes still re-apply."""
        wd = _wd(make_settings({}))
        wd._dynamic_routes = {
            "a": ("8995", {"@id": "a", "match": [{"path": ["/a"]}]}),
            "b": ("9999", {"@id": "b", "match": [{"path": ["/b"]}]}),  # gone
        }
        client = _FakeAsyncClient(
            request_responder=_servers_responder(
                {"srv0": {"listen": ["0.0.0.0:8995"]}}
            )
        )
        await wd.load_config("cf", client=client)  # must not raise
        posts = [
            r
            for r in client.requests
            if r[0] == "POST"
            and r[1] == "http://localhost/config/apps/http/servers/srv0/routes"
        ]
        assert {p[2]["@id"] for p in posts} == {"a"}  # only the live one

    @pytest.mark.asyncio
    async def test_reapply_swallows_individual_failure(self):
        """One route's re-POST failing (non-404) doesn't abort the rest."""
        wd = _wd(make_settings({}))
        wd._dynamic_routes = {
            "a": ("8995", {"@id": "a"}),
            "b": ("8995", {"@id": "b"}),
        }

        # First POST (route a) 500s; second (route b) 200s.
        call = {"n": 0}

        def _r(m, u, b):
            if m == "GET":
                return _FakeResponse(
                    200, json_data={"srv0": {"listen": ["0.0.0.0:8995"]}}
                )
            call["n"] += 1
            return _FakeResponse(500 if call["n"] == 1 else 200)

        client = _FakeAsyncClient(request_responder=_r)
        await wd.load_config("cf", client=client)  # must not raise

    @pytest.mark.asyncio
    async def test_load_owns_and_closes_client_when_none_injected(
        self, monkeypatch
    ):
        """load_config with client=None builds one UDS client for both the
        POST /load and the re-apply, then closes it."""
        import klangk.caddy as caddy_mod

        wd = _wd(make_settings({}))
        wd._dynamic_routes = {
            "a": ("8995", {"@id": "a", "match": [{"path": ["/a"]}]}),
        }
        monkeypatch.setattr(
            caddy_mod.httpx, "AsyncHTTPTransport", lambda uds: "t"
        )

        class _C(_FakeAsyncClient):
            def __init__(self, *a, **k):
                k.setdefault(
                    "request_responder",
                    _servers_responder({"srv0": {"listen": ["0.0.0.0:8995"]}}),
                )
                super().__init__(*a, **k)

        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _C)
        await wd.load_config("cf")
        assert (
            _FakeAsyncClient.instances
            and _FakeAsyncClient.instances[-1].closed
        )
