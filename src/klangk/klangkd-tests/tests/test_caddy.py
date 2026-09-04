"""Unit tests for the Caddy proxy engine (#1559).

Parallel to ``test_proxy.py`` (the nginx engine). These exercise the pure
Caddyfile rendering logic + the admin-API client / watchdog orchestration
without a running Caddy — the runtime enforcement (spawn/respawn, ACLs,
forward_auth) is covered by the e2e suite (``test_caddy_*_e2e.py``, run under
devenv where the ``caddy`` binary is present; CI's plain-pip unit job has no
caddy, so nothing here shells out to it).
"""

import asyncio
import logging
import os
import signal
import sys
import tempfile
import types
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from klangk.caddy import (
    CaddyRenderer,
    CaddyWatchdog,
    CSP_POLICY,
    classify_caddy_line,
    csp_block,
    is_bind_error,
)
from klangk.caddy import (
    CADDYFILE_CONTENT_TYPE,
    _caddy_parseable_cidr,
    post_load,
    tcp_upstream,
    uds_upstream,
)
from _helpers import make_settings, tracked_mkdtemp


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
        s = make_settings({"KLANGKD_FILE_UPLOAD_SIZE_MAX": "10485760"})
        assert _renderer(s)._max_body_size() == "10MB"

    def test_minimum_1mb(self):
        s = make_settings({"KLANGKD_FILE_UPLOAD_SIZE_MAX": "100"})
        assert _renderer(s)._max_body_size() == "1MB"

    def test_garbage_rejected_at_startup(self):
        # #2603: a malformed upload cap aborts construction (naming the
        # field) instead of silently falling back to 500MB at render time.
        with pytest.raises(Exception, match="file_upload_size_max"):
            make_settings({"KLANGKD_FILE_UPLOAD_SIZE_MAX": "not-a-number"})


class TestContainerSourceLists:
    def test_egress_list_includes_all_sources(self):
        s = make_settings(
            env={"KLANGKD_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        lst = _renderer(s)._egress_remote_ip_list()
        assert "10.89.0.0/24" in lst
        # loopback included in the egress allow set
        assert "127.0.0.1" in lst

    def test_browser_deny_list_excludes_loopback(self):
        s = make_settings(
            env={"KLANGKD_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        lst = _renderer(s)._browser_deny_remote_ip_list()
        assert "10.89.0.0/24" in lst
        assert "127.0.0.1" not in lst

    def test_all_loopback_warns(self, caplog):
        s = make_settings({"KLANGKD_CONTAINER_SUBNETS": "127.0.0.1"})
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

    def test_invalid_container_subnet_skipped(self, caplog):
        """A typo'd CIDR is warned and skipped, not rendered into the
        Caddyfile (Caddy would reject the config at adapt time and wedge
        the proxy in a kill/respawn loop)."""
        s = make_settings(
            {"KLANGKD_CONTAINER_SUBNETS": "notacidr,10.89.0.0/24"}
        )
        with caplog.at_level("WARNING"):
            acl, deny = _renderer(s)._container_source_entries()
        assert "10.89.0.0/24" in acl
        assert "notacidr" not in acl
        assert "notacidr" not in deny
        assert "invalid IP/CIDR entry" in caplog.text

    def test_all_invalid_container_subnets_fail_closed(self, caplog):
        """Every entry invalid -> warn + deny-all (the blank-setting
        semantic), not a wedged proxy and not a silent widening to
        auto-detected sources."""
        s = make_settings({"KLANGKD_CONTAINER_SUBNETS": "garbage"})
        with caplog.at_level("WARNING"):
            lst = _renderer(s)._egress_remote_ip_list()
        assert lst == ""
        assert "invalid entry" in caplog.text

    def test_invalid_trusted_proxy_cidr_skipped(self, caplog):
        s = make_settings({"KLANGKD_TRUSTED_PROXY_CIDRS": "oops,10.0.0.0/8"})
        with caplog.at_level("WARNING"):
            cidrs = _renderer(s)._trusted_proxy_cidrs()
        assert cidrs == ["10.0.0.0/8"]
        assert "invalid IP/CIDR entry" in caplog.text

    def test_all_invalid_trusted_proxy_cidrs_loopback(self, caplog):
        s = make_settings({"KLANGKD_TRUSTED_PROXY_CIDRS": "oops"})
        with caplog.at_level("WARNING"):
            cidrs = _renderer(s)._trusted_proxy_cidrs()
        assert cidrs == ["127.0.0.1", "::1"]
        assert "invalid IP/CIDR entry" in caplog.text

    @pytest.mark.parametrize(
        "entry",
        [
            "10.0.0.0/255.255.0.0",  # dotted-quad netmask: python yes, netip no
            "10.0.0.0/0.0.255.255",  # hostmask form: same mismatch
            "10.0.0.0/\u0661\u0662",  # Arabic-Indic digits: isdigit() yes, ParseUint no
        ],
    )
    def test_python_only_cidr_forms_rejected(self, entry, caplog):
        """The accept boundary must match Caddy's provisioner (Go netip),
        not Python's ipaddress: netmask/hostmask notation and non-ASCII
        digit suffixes parse in Python but fail at POST /load provision
        time — the kill/respawn wedge this validator exists to prevent
        (nginx accepts netmask notation, so copy-pasted lines hit it)."""
        assert _caddy_parseable_cidr(entry) is False
        s = make_settings(
            {"KLANGKD_CONTAINER_SUBNETS": f"{entry},192.168.0.0/16"}
        )
        with caplog.at_level("WARNING"):
            acl, _deny = _renderer(s)._container_source_entries()
        assert entry not in acl
        assert "192.168.0.0/16" in acl
        assert "invalid IP/CIDR entry" in caplog.text

    def test_host_bits_cidr_still_accepted(self):
        """Go netip.ParsePrefix accepts host-bits-set prefixes (masking
        them), so 10.0.0.1/24 must stay valid — do not over-reject."""
        assert _caddy_parseable_cidr("10.0.0.1/24") is True
        assert _caddy_parseable_cidr("10.0.0.0/8") is True
        assert _caddy_parseable_cidr("127.0.0.1") is True
        assert _caddy_parseable_cidr("::1") is True
        assert _caddy_parseable_cidr("fe80::/10") is True
        assert _caddy_parseable_cidr("notacidr") is False


# ---------------------------------------------------------------------------
# CaddyRenderer — section builders
# ---------------------------------------------------------------------------


class TestCaddySupportsFullGlobalBlock:
    """caddy_supports_full_global_block probes the binary so klangkd's config
    loads on both the devenv's current caddy and the older system caddy a stock
    CI runner apt-installs (Ubuntu 24.04 -> 2.6.2; #1709)."""

    def test_true_when_adapt_succeeds(self, monkeypatch):
        from klangk import caddy as caddy_mod

        class _R:
            returncode = 0

        monkeypatch.setattr(caddy_mod.subprocess, "run", lambda *a, **k: _R())
        assert caddy_mod.caddy_supports_full_global_block("/x/caddy") is True

    def test_false_when_adapt_rejects_the_probe(self, monkeypatch):
        from klangk import caddy as caddy_mod

        class _R:
            returncode = (
                1  # e.g. 2.6.2: "unrecognized global option: persist_config"
            )

        monkeypatch.setattr(caddy_mod.subprocess, "run", lambda *a, **k: _R())
        assert caddy_mod.caddy_supports_full_global_block("/x/caddy") is False

    def test_true_when_probe_cannot_run(self, monkeypatch):
        from klangk import caddy as caddy_mod

        def _boom(*a, **k):
            raise OSError("no such binary")

        monkeypatch.setattr(caddy_mod.subprocess, "run", _boom)
        # conservative default: assume supported (rare; preserves features)
        assert caddy_mod.caddy_supports_full_global_block("/nope") is True


class TestGlobalBlock:
    def test_admin_uds_autohttps_persist(self):
        s = make_settings({"KLANGKD_PORT": "8997"})
        g = _renderer(s)._global_block("/d/caddy-admin.sock")
        assert "admin unix///d/caddy-admin.sock" in g
        assert (
            "|0600" not in g
        )  # mode enforced via os.chmod, not the address (#1709)
        assert "auto_https off" in g
        assert "persist_config off" in g

    def test_admin_address_has_no_mode_suffix(self):
        """The admin address carries NO |<mode> suffix: that syntax is only
        honored on Caddy >= 2.8, and on older Caddy it's folded into the
        socket path, breaking the bind (#1709). Owner-only mode (0600) is
        enforced by the watchdog via os.chmod instead (see CaddyWatchdog.
        _wait_for_admin); #1559's locked decision stands, just not via the
        address. Regression guard against re-adding the version-fragile
        suffix."""
        g = _renderer(make_settings({"KLANGKD_PORT": "8997"}))._global_block(
            "/d/caddy-admin.sock"
        )
        assert "|0600" not in g
        assert "|0660" not in g
        assert "|0644" not in g
        assert "admin unix///d/caddy-admin.sock" in g

    def test_bootstrap_block_is_admin_only(self):
        """The initial --config carries only the admin global option, so Caddy
        binds the admin UDS at bootstrap on any version — not /dev/null (which
        falls back to localhost:2019 on Caddy < 2.7, #1709). Site config arrives
        later via POST /load, so auto_https / persist_config / trusted_proxies
        are deliberately absent here."""
        b = _renderer(
            make_settings({"KLANGKD_PORT": "8997"})
        )._bootstrap_block("/d/caddy-admin.sock")
        # Establishes the admin endpoint (no |0600 suffix — see
        # test_admin_address_has_no_mode_suffix; mode is chmod'd at runtime).
        assert "admin unix///d/caddy-admin.sock" in b
        assert "|0600" not in b
        assert b.startswith("{\n") and b.rstrip().endswith("}")
        # Site/global knobs are absent — they come via /load, not the bootstrap.
        assert "auto_https" not in b
        assert "persist_config" not in b
        assert "trusted_proxies" not in b
        assert "reverse_proxy" not in b

    def test_trusted_proxies_present_by_default(self):
        s = make_settings(
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,127.0.0.1",
            }
        )
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies static 10.0.0.0/8 127.0.0.1" in g
        assert "trusted_proxies_strict" in g

    def test_minimal_global_block_when_full_global_false(self):
        """On older system caddy (post-2.6.2 lacks persist_config and
        servers/trusted_proxies; e.g. Ubuntu 24.04's apt caddy 2.6.2), the full
        global block is rejected outright (#1709). When full_global=False the
        block degrades to admin + auto_https only -- no persist_config, no
        servers/trusted_proxies/strict. CaddyWatchdog.start's probe decides
        which path."""
        s = make_settings({"KLANGKD_PORT": "8997"})
        g = _renderer(s)._global_block("/d/sock", full_global=False)
        assert "auto_https off" in g
        assert "persist_config" not in g
        assert "trusted_proxies" not in g
        assert "servers" not in g

    def test_trusted_proxies_suppressed_when_reject(self):
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_REJECT_PROXY_HEADERS": "1"}
        )
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies" not in g

    def test_trusted_proxies_defaults_to_loopback(self):
        s = make_settings({"KLANGKD_PORT": "8997"})
        g = _renderer(s)._global_block("/d/sock")
        assert "trusted_proxies static 127.0.0.1 ::1" in g

    def test_trusted_proxies_empty_falls_back_to_loopback(self):
        """All-empty/commas KLANGKD_TRUSTED_PROXY_CIDRS → loopback fallback."""
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_TRUSTED_PROXY_CIDRS": ",,"}
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
    """The /llm-proxy/ block routes to the klangkd backend (#2073)."""

    def test_always_emits_block(self):
        """The LLM block is always emitted (no llm_base_url conditional)."""
        b = _renderer(make_settings({}))._build_llm_block("upstream", "")
        assert "handle /llm-proxy/*" in b
        assert "reverse_proxy upstream" in b

    def test_includes_guard(self):
        guard = "\t\trespond @notContainerSrc 403\n"
        b = _renderer(make_settings({}))._build_llm_block("upstream", guard)
        assert "respond @notContainerSrc 403" in b

    def test_no_api_key_injection(self):
        """No Authorization header — the in-process Router handles keys."""
        s = make_settings(env={"KLANGKD_LLM_API_KEY": "sekret"})
        b = _renderer(s)._build_llm_block("upstream", "")
        assert "Authorization" not in b

    def test_no_rewrite(self):
        """No URL rewriting — the backend handles /llm-proxy/ directly."""
        b = _renderer(make_settings({}))._build_llm_block("upstream", "")
        assert "rewrite" not in b


class TestHostedBlock:
    def test_disabled_when_zero(self):
        s = make_settings({"KLANGKD_HOSTED_PORTS_PER_WORKSPACE": "0"})
        b = _renderer(s)._build_hosted_block()
        assert "respond 404" in b
        assert "reverse_proxy 127.0.0.1" not in b

    def test_enabled_emits_redirect_and_proxy(self):
        s = make_settings({"KLANGKD_PORT": "8997"})
        b = _renderer(s)._build_hosted_block()
        assert "path_regexp hostedsl" in b
        assert "redir {uri}/ 308" in b
        assert "path_regexp hosted" in b
        assert "reverse_proxy 127.0.0.1:{re.hosted.1}" in b


# ---------------------------------------------------------------------------
# CaddyRenderer — render_config compiles via `caddy adapt` (smoke)
# ---------------------------------------------------------------------------


class TestLlmBlockCaddyAdapt:
    """Smoke-test that the simplified /llm-proxy block compiles via caddy adapt.

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

    @pytest.fixture(autouse=True)
    def _skip_without_caddy(self):
        import shutil

        if not shutil.which("caddy"):
            pytest.skip("no `caddy` binary on PATH (run under devenv)")

    @staticmethod
    def _adapt(cf: str) -> dict:
        """Run `caddy adapt --adapter caddyfile` on a rendered config; return JSON."""
        import json
        import subprocess

        with tempfile.NamedTemporaryFile(
            "w", suffix=".Caddyfile", delete=False
        ) as f:
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

    def test_llm_block_compiles(self):
        """The simplified /llm-proxy block compiles through caddy adapt."""
        s = make_settings({})
        cf = _renderer(s).render_config("unix//s", "/d/caddy-admin.sock")
        self._adapt(cf)


# ---------------------------------------------------------------------------
# CaddyRenderer — render_config (structure)
# ---------------------------------------------------------------------------


class TestCspBlock:
    """The frontend hardening headers (#3149): CSP + X-Frame-Options on the
    browser listener's frontend paths, excluded from API/WS/hosted, absent
    from the egress listener."""

    def test_block_shape(self):
        b = csp_block()
        assert (
            "@frontend not path /api /api/* /ws /ws/* /hosted /hosted/*" in b
        )
        assert f'header @frontend Content-Security-Policy "{CSP_POLICY}"' in b
        assert 'header @frontend X-Frame-Options "DENY"' in b

    def test_policy_is_first_party(self):
        # No third-party origins, no scheme-source widening, and no script
        # eval: fonts are self-hosted (#3149 additional scope), same-origin
        # ws/wss is covered by 'self' (CSP3), and no shipped feature
        # JS-evals.
        assert "https://" not in CSP_POLICY
        assert "ws:" not in CSP_POLICY
        assert "wss:" not in CSP_POLICY
        assert "unsafe-eval;" not in CSP_POLICY  # wasm-unsafe-eval only
        assert "fonts.gstatic.com" not in CSP_POLICY
        # The clickjacking posture.
        assert "frame-ancestors 'none'" in CSP_POLICY

    def test_browser_site_carries_headers(self):
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config("unix//s", "/d/a.sock")
        browser = cf[cf.index("http://:8997 {") :]
        assert "@frontend not path" in browser
        assert CSP_POLICY in browser

    def test_egress_site_has_no_headers(self):
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config("unix//s", "/d/a.sock")
        egress = cf[cf.index("http://:8995 {") : cf.index("http://:8997 {")]
        assert "Content-Security-Policy" not in egress
        assert "X-Frame-Options" not in egress

    def test_headless_render_has_no_headers(self):
        # No KLANGKD_PORT -> headless (egress listener only, no browser
        # listener) -> nothing serves documents -> no CSP anywhere.
        s = make_settings({"KLANGKD_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//s", "/d/a.sock")
        assert "Content-Security-Policy" not in cf


class TestRenderConfig:
    ADMIN = "/d/caddy-admin.sock"

    def test_full_has_two_listeners(self):
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config(
            tcp_upstream("127.0.0.1", "8997"), self.ADMIN
        )
        # browser + egress site blocks
        assert "http://:8997 {" in cf
        assert "http://:8995 {" in cf
        assert "bind 127.0.0.1" in cf
        assert "bind 0.0.0.0" in cf

    def test_egress_proxies_sidecar_ws(self):
        # #2319: the sidecar's egress-sidecar WebSocket has an explicit handle
        # on the egress site so its WS upgrade is reverse-proxied to the app
        # (+ container-src-guarded) instead of falling through to the catch-all
        # StaticFiles (which asserts scope["type"]=="http" -> 500).
        s = make_settings(env={"KLANGKD_EGRESS_PORT": "8995"})
        locs = _renderer(s)._egress_locations("upstream", "10.0.0.0/8")
        assert "handle /ws/egress-sidecar" in locs
        assert (
            "flush_interval -1" in locs
        )  # unbuffered bidirectional verdict stream
        assert "reverse_proxy upstream" in locs

    def test_headless_has_only_egress(self):
        s = make_settings(env={"KLANGKD_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//sock", self.ADMIN)
        assert "http://:8995 {" in cf
        assert "http://:8997" not in cf
        assert "bind 0.0.0.0" in cf

    def test_template_keys_off_port_not_auth(self):
        for auth in ("none", "password", "both"):
            sh = make_settings(
                env={"KLANGKD_AUTH_MODES": auth, "KLANGKD_EGRESS_PORT": "8995"}
            )
            assert "http://:8997" not in _renderer(sh).render_config(
                "unix//s", self.ADMIN
            )
            sf = make_settings(
                env={
                    "KLANGKD_AUTH_MODES": auth,
                    "KLANGKD_PORT": "8997",
                    "KLANGKD_EGRESS_PORT": "8995",
                }
            )
            assert "http://:8997 {" in _renderer(sf).render_config(
                "unix//s", self.ADMIN
            )

    def test_forward_auth_present(self):
        s = make_settings(env={"KLANGKD_EGRESS_PORT": "8995"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # WS upgrades bypass forward_auth: Caddy copies the Upgrade headers onto
        # the auth subrequest, making IT a websocket so uvicorn routes the HTTP
        # verify endpoint as a WS -> no match -> StaticFiles 500. The @notWs
        # matcher excludes WS upgrades; the egress WS endpoint self-authenticates
        # via the Authorization header instead.
        assert "@notWs {" in cf
        assert "not header Upgrade websocket" in cf
        assert "forward_auth @notWs unix//s {" in cf
        assert "uri /api/v1/auth/verify-workspace-token" in cf

    def test_request_body_max_size(self):
        s = make_settings(
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_FILE_UPLOAD_SIZE_MAX": "10485760",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "max_size 10MB" in cf

    def test_auth_local_loopback_acl(self):
        s = make_settings({"KLANGKD_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # nginx uses ``location =`` (exact); Caddy mirrors with a path matcher.
        assert "@authlocal path /api/v1/auth/local" in cf
        assert "handle @authlocal {" in cf
        assert "@notLoopback not remote_ip 127.0.0.1 ::1" in cf
        assert "respond @notLoopback 403" in cf

    def test_auth_local_is_exact_match(self):
        """/auth/local uses an exact path matcher (nginx ``location =``), so a
        sub-path like /api/v1/auth/local/other does NOT match the handle."""
        s = make_settings({"KLANGKD_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "path /api/v1/auth/local\n" in cf
        # no trailing /* (which would make it prefix)
        assert "path /api/v1/auth/local/*" not in cf

    def test_browser_catch_all_container_deny(self):
        s = make_settings(
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_CONTAINER_SUBNETS": "10.89.0.0/24",
            }
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
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_CONTAINER_SUBNETS": "10.89.0.0/24",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@containerSrc remote_ip 10.89.0.0/24" in cf
        # No container-source deny keyed on client_ip anywhere.
        assert "@containerSrc client_ip" not in cf
        assert "not client_ip" not in cf

    def test_egress_acl_uses_remote_ip(self):
        s = make_settings(
            env={
                "KLANGKD_EGRESS_PORT": "8995",
                "KLANGKD_CONTAINER_SUBNETS": "10.89.0.0/24",
            }
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@notContainerSrc not remote_ip 10.89.0.0/24" in cf

    def test_egress_fail_closed_when_no_container_sources(self, monkeypatch):
        """Whitespace-only KLANGKD_CONTAINER_SUBNETS → no sources → egress
        fails closed (deny all), matching nginx's bare ``deny all;``."""
        import klangk.caddy as caddy_mod

        monkeypatch.setattr(caddy_mod, "detect_host_ipv4s", lambda: [])
        s = make_settings(
            env={
                "KLANGKD_EGRESS_PORT": "8995",
                "KLANGKD_CONTAINER_SUBNETS": "   ",
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
            {"KLANGKD_PORT": "8997", "KLANGKD_CONTAINER_SUBNETS": "127.0.0.1"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "@containerSrc" not in cf
        assert "respond @containerSrc 403" not in cf
        # The catch-all still proxies.
        assert "handle {" in cf

    def test_llm_block_always_present(self):
        """The /llm-proxy block is always rendered (#2073)."""
        s = make_settings({"KLANGKD_PORT": "8997"})
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        assert "/llm-proxy" in cf

    def test_global_block_prepended(self):
        s = make_settings(
            {"KLANGKD_PORT": "8997", "KLANGKD_EGRESS_PORT": "8995"}
        )
        cf = _renderer(s).render_config("unix//s", self.ADMIN)
        # global block is first, before any site block.
        admin_pos = cf.index("admin unix//")
        first_site = cf.index("http://")
        assert admin_pos < first_site


class TestFindProxyBin:
    def test_configured(self):
        s = make_settings({"KLANGKD_PROXY_BIN": "/custom/caddy"})
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
        # On macOS, pytest tmp_path resolves through /private/var/folders/...
        # which can exceed the 104-byte AF_UNIX sun_path limit. Use a short
        # temp dir so the socket path passes the settings validator (#1983).
        if sys.platform == "darwin":
            state = tracked_mkdtemp("ks-")
            sock = os.path.join(state, "caddy-admin.sock")
            s = make_settings(
                {
                    "KLANGKD_STATE_DIR": state,
                    "KLANGKD_CADDY_ADMIN_SOCKET": sock,
                }
            )
        else:
            state = str(tmp_path)
            sock = str(tmp_path / "caddy-admin.sock")
            s = make_settings({"KLANGKD_STATE_DIR": state})
        wd = _wd(s)
        assert wd.admin_socket == sock

    def test_admin_socket_override(self, tmp_path):
        """KLANGKD_CADDY_ADMIN_SOCKET overrides the default path (#1636) — read
        live off settings, not built inline."""
        s = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path),
                "KLANGKD_CADDY_ADMIN_SOCKET": "/short/caddy-admin.sock",
            }
        )
        wd = _wd(s)
        assert wd.admin_socket == "/short/caddy-admin.sock"
        assert wd.admin_bind_address == "unix///short/caddy-admin.sock"

    def test_admin_bind_address_has_no_mode_suffix(self, tmp_path):
        """The Caddy bind address is the bare unix//<path> with NO |0600 mode
        suffix (that's version-fragile — #1709; mode enforced via os.chmod).
        The bare path (admin_socket) is what httpx dials."""
        if sys.platform == "darwin":
            state = tracked_mkdtemp("ks-")
            sock = os.path.join(state, "caddy-admin.sock")
            s = make_settings(
                {
                    "KLANGKD_STATE_DIR": state,
                    "KLANGKD_CADDY_ADMIN_SOCKET": sock,
                }
            )
        else:
            state = str(tmp_path)
            sock = str(tmp_path / "caddy-admin.sock")
            s = make_settings({"KLANGKD_STATE_DIR": state})
        wd = _wd(s)
        assert wd.admin_bind_address == f"unix//{sock}"
        assert "|0600" not in wd.admin_bind_address
        assert wd.admin_socket == sock

    def test_find_proxy_bin_delegates_to_renderer(self, monkeypatch):
        s = make_settings({"KLANGKD_PROXY_BIN": "/x/caddy"})
        assert _wd(s).find_proxy_bin() == "/x/caddy"


class TestWatchdogLoadConfig:
    @pytest.mark.asyncio
    async def test_renders_and_posts(self, monkeypatch):
        """load_config renders the Caddyfile (UDS upstream) and POSTs it."""
        s = make_settings(env={"KLANGKD_EGRESS_PORT": "8995"})
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

    @pytest.mark.asyncio
    async def test_transport_timeout_retried_not_fatal(self, monkeypatch):
        """A stalled admin peer (accepts, never answers -> ReadTimeout) is
        a not-yet-up poll, not an exception that kills the watchdog task
        and leaves a blank-config Caddy unsupervised (#3123)."""
        import klangk.caddy as caddy_mod

        async def _fake_sleep(s):
            pass

        monkeypatch.setattr(caddy_mod.asyncio, "sleep", _fake_sleep)

        class _Stalled(_FakeAsyncClient):
            async def get(self, url):
                raise httpx.ReadTimeout("stalled")

        monkeypatch.setattr(
            caddy_mod.httpx, "AsyncHTTPTransport", lambda uds: None
        )
        monkeypatch.setattr(caddy_mod.httpx, "AsyncClient", _Stalled)
        wd = _wd(make_settings({}))
        assert await wd._wait_for_admin(timeout=0.001) is False


class TestWatchdogStart:
    @pytest.mark.asyncio
    async def test_start_noop_when_disabled(self, monkeypatch):
        monkeypatch.setenv("_KLANGKD_DISABLE_PROXY", "1")
        wd = _wd(make_settings({}))
        await wd.start()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_start_runs_prepare_and_spawns(self, monkeypatch, tmp_path):
        """When enabled, start() resolves the bin + schedules the watchdog."""
        # On macOS, pytest tmp_path can exceed the AF_UNIX sun_path limit;
        # use a short temp dir for the state + socket (#1983).
        if sys.platform == "darwin":
            state = tracked_mkdtemp("ks-")
        else:
            state = str(tmp_path)
        s = make_settings(
            env={
                "KLANGKD_STATE_DIR": state,
                "KLANGKD_SOCKET": os.path.join(state, "klangk.sock"),
                "KLANGKD_EGRESS_PORT": "19999",
            }
        )
        monkeypatch.delenv("_KLANGKD_DISABLE_PROXY", raising=False)
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
# classify_caddy_line
# ---------------------------------------------------------------------------


class TestClassifyCaddyLine:
    """Caddy stderr JSON lines are classified into Python log levels."""

    def test_info_maps_to_debug(self):
        line = '{"level":"info","ts":1,"msg":"serving initial configuration"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.DEBUG
        assert "serving initial configuration" in msg

    def test_warn_maps_to_debug(self):
        line = '{"level":"warn","ts":1,"msg":"HTTP/2 skipped because it requires TLS"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.DEBUG

    def test_error_maps_to_error(self):
        line = '{"level":"error","ts":1,"msg":"listener closed"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.ERROR
        assert "listener closed" in msg

    def test_fatal_maps_to_error(self):
        line = '{"level":"fatal","ts":1,"msg":"caddy process crash"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.ERROR

    def test_panic_maps_to_error(self):
        line = '{"level":"panic","ts":1,"msg":"unexpected nil pointer"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.ERROR

    def test_logger_field_included_in_message(self):
        line = '{"level":"info","ts":1,"logger":"admin.api","msg":"received request"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.DEBUG
        assert "[admin.api] received request" == msg

    def test_non_json_treated_as_error(self):
        line = "panic: runtime error: index out of range"
        level, msg = classify_caddy_line(line)
        assert level == logging.ERROR
        assert msg == line

    def test_missing_level_defaults_to_debug(self):
        line = '{"ts":1,"msg":"something"}'
        level, msg = classify_caddy_line(line)
        assert level == logging.DEBUG

    def test_missing_msg_falls_back_to_raw_line(self):
        line = '{"level":"error","ts":1}'
        level, msg = classify_caddy_line(line)
        assert level == logging.ERROR
        assert msg == line

    def test_http_address_stays_debug(self):
        line = (
            '{"level":"debug","ts":1,"logger":"http",'
            '"msg":"starting server loop","address":":8995","tls":false}'
        )
        level, msg = classify_caddy_line(line)
        assert level == logging.DEBUG


# ---------------------------------------------------------------------------
# CaddyWatchdog._log_listeners
# ---------------------------------------------------------------------------


class TestLogListeners:
    """_log_listeners logs browser and/or egress ports at INFO."""

    def _make_watchdog(
        self,
        *,
        port=None,
        listen="127.0.0.1",
        egress_port="8995",
        egress_listen="0.0.0.0",
    ):
        app = Mock()
        app.state.settings.port = port
        app.state.settings.listen = listen
        app.state.settings.egress_port = egress_port
        app.state.settings.egress_listen = egress_listen
        return CaddyWatchdog(app)

    def test_logs_both_ports(self, caplog):
        wd = self._make_watchdog(port="8997", egress_port="8995")
        with caplog.at_level(logging.INFO, logger="klangk.caddy"):
            wd._log_listeners()
        assert "caddy ingress listening on 127.0.0.1:8997" in caplog.text
        assert "caddy egress listening on 0.0.0.0:8995" in caplog.text

    def test_logs_egress_only_when_port_is_none(self, caplog):
        wd = self._make_watchdog(port=None, egress_port="8995")
        with caplog.at_level(logging.INFO, logger="klangk.caddy"):
            wd._log_listeners()
        assert "browser" not in caplog.text
        assert "caddy egress listening on 0.0.0.0:8995" in caplog.text


# ---------------------------------------------------------------------------
# is_bind_error (#1917)
# ---------------------------------------------------------------------------


class TestIsBindError:
    """Detect Caddy bind failures from structured JSON stderr lines."""

    def test_admin_bind_address_in_use(self):
        line = (
            '{"level":"error","logger":"admin",'
            '"msg":"listen unix //tmp/caddy.sock: bind: address already in use"}'
        )
        assert is_bind_error(line) is True

    def test_http_bind_address_in_use(self):
        line = (
            '{"level":"error","logger":"http",'
            '"msg":"listen tcp :8443: bind: address already in use"}'
        )
        assert is_bind_error(line) is True

    def test_bind_permission_denied(self):
        line = (
            '{"level":"error","logger":"admin",'
            '"msg":"listen unix //run/caddy.sock: bind: permission denied"}'
        )
        assert is_bind_error(line) is True

    def test_non_bind_error_is_false(self):
        line = '{"level":"error","logger":"admin","msg":"listener closed"}'
        assert is_bind_error(line) is False

    def test_info_level_is_false(self):
        line = (
            '{"level":"info","logger":"admin",'
            '"msg":"admin endpoint started, bind to unix socket"}'
        )
        assert is_bind_error(line) is False

    def test_non_json_is_false(self):
        assert is_bind_error("panic: something broke") is False

    def test_no_msg_field_is_false(self):
        line = '{"level":"error","logger":"admin"}'
        assert is_bind_error(line) is False


# ---------------------------------------------------------------------------
# CaddyWatchdog._bind_fatal flag (#1917)
# ---------------------------------------------------------------------------


class TestWatchdogBindFatal:
    """The watchdog aborts instead of respawning on bind errors."""

    def test_init_flag_is_false(self):
        app = Mock()
        app.state.settings.state_dir = "/tmp"
        wd = CaddyWatchdog(app)
        assert wd._bind_fatal is False


class TestCaddyHostIpv4Detection:
    """detect_host_ipv4s lives in klangk.caddy (#2088); caddy's own render
    tests monkeypatch it, so exercise the real impl here for coverage."""

    def test_detect_host_ipv4s_parses_inet_lines(self, monkeypatch):
        from klangk import caddy as caddy_mod

        monkeypatch.setattr(
            caddy_mod.subprocess,
            "check_output",
            lambda *a, **k: (
                "    inet 127.0.0.1/8 scope host lo\n"
                "    inet 192.168.1.5/24 brd 192.168.1.255\n"
            ),
        )
        assert caddy_mod.detect_host_ipv4s() == [
            "127.0.0.1",
            "192.168.1.5",
        ]

    def test_detect_host_ipv4s_failure_returns_empty(self, monkeypatch):
        from klangk import caddy as caddy_mod

        def _raise(*a, **k):
            raise FileNotFoundError("no ip")

        monkeypatch.setattr(caddy_mod.subprocess, "check_output", _raise)
        assert caddy_mod.detect_host_ipv4s() == []
