"""Unit tests for the nginx config renderer (#1396).

These exercise the pure rendering logic (config generation) without running a
real nginx — the runtime ACL enforcement is covered by the e2e suite
(``test_nginx_acl_e2e.py``).
"""

import asyncio
from unittest.mock import Mock

import pytest

import types

from klangk_backend.nginx import (
    NginxRenderer,
    detect_host_ipv4s,
    tcp_upstream,
    uds_upstream,
)
from _helpers import make_settings
from klangk_backend.settings import KlangkSettings


def _renderer(settings):
    """Wrap settings in a minimal app_state and build a NginxRenderer (#1469)."""
    return NginxRenderer(types.SimpleNamespace(settings=settings))


def _wd(settings):
    """Build a NginxWatchdog from settings (wrapped in a minimal app_state)."""
    from klangk_backend.nginx import NginxWatchdog

    return NginxWatchdog(types.SimpleNamespace(settings=settings))


class TestUpstreams:
    def test_tcp_upstream(self):
        assert tcp_upstream("127.0.0.1", "8997") == "http://127.0.0.1:8997"

    def test_uds_upstream(self):
        assert uds_upstream("/tmp/sock") == "http://unix:/tmp/sock:"


class TestClientMaxBodySize:
    def test_default_500mb(self):
        s = make_settings({})
        assert _renderer(s).compute_client_max_body_size() == "500m"

    def test_custom(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "10485760"})
        assert _renderer(s).compute_client_max_body_size() == "10m"

    def test_minimum_1m(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "100"})
        assert _renderer(s).compute_client_max_body_size() == "1m"

    def test_garbage_falls_back(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "not-a-number"})
        assert _renderer(s).compute_client_max_body_size() == "500m"


class TestContainerAcls:
    def test_explicit_subnets(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16"}
        )
        acl, deny = _renderer(s).compute_container_acls()
        assert "allow 10.89.0.0/24;" in acl
        assert "allow 172.30.0.0/16;" in acl
        assert "deny all;" in acl
        # 127.0.0.1 NOT implicitly added on explicit override.
        assert "allow 127.0.0.1;" not in acl
        # Deny block denies the non-loopback subnets.
        assert "deny 10.89.0.0/24;" in deny
        assert "allow all;" in deny

    def test_loopback_excluded_from_deny(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        _, deny = _renderer(s).compute_container_acls()
        assert "deny 10.89.0.0/24;" in deny
        assert "deny 127.0.0.1;" not in deny
        assert "allow all;" in deny

    def test_all_loopback_warns(self, caplog):
        s = make_settings({"KLANGK_CONTAINER_SUBNETS": "127.0.0.1"})
        _, deny = _renderer(s).compute_container_acls()
        assert "allow all;" in deny
        assert "no non-loopback" in caplog.text

    def test_auto_detect_or_fallback(self):
        """Without explicit subnets: either host IPs or fallback RFC1918."""
        s = make_settings({})
        acl, deny = _renderer(s).compute_container_acls()
        # Must produce *something* (host IPs or fallback).
        assert "deny all;" in acl
        assert "allow all;" in deny


class TestDnsResolvers:
    def test_explicit_servers(self):
        s = make_settings({"KLANGK_DNS_SERVERS": "1.2.3.4,5.6.7.8"})
        result = _renderer(s).detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_ipv6_bracketed(self):
        s = make_settings({"KLANGK_DNS_SERVERS": "::1"})
        assert "[::1]" in _renderer(s).detect_dns_resolvers()

    def test_empty_tokens_skipped(self):
        """Trailing commas / empty entries in KLANGK_DNS_SERVERS are skipped."""
        s = make_settings({"KLANGK_DNS_SERVERS": "1.2.3.4,,5.6.7.8,"})
        result = _renderer(s).detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_fallback(self):
        s = make_settings({})
        result = _renderer(s).detect_dns_resolvers()
        assert len(result) > 0  # from resolv.conf or 8.8.8.8


class TestRenderConfig:
    def test_basic_structure(self):
        # KLANGK_PORT set ⇒ full/browser template.
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "daemon off;" in conf
        # Browser listener (listen {listen}:{port}) + egress listener.
        assert "listen 127.0.0.1:8997;" in conf
        assert "listen 8995;" in conf
        assert "proxy_pass http://127.0.0.1:8997" in conf
        # Core locations present (split across the two server blocks).
        assert "location /api/v1/browser-delegate" in conf
        assert "location = /api/v1/auth/local" in conf
        assert "location /" in conf

    def test_uds_upstream_in_conf(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf

    def test_no_llm_block_without_url(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" not in conf

    def test_llm_block_with_url(self):
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" in conf

    def test_llm_api_key_resolved(self):
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "cmd:printf %s resolved-key",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer resolved-key"' in conf
        assert "cmd:" not in conf

    def test_auth_local_loopback_acl(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # Find the auth/local block.
        import re

        m = re.search(
            r"location = /api/v1/auth/local \{(.*?)\}", conf, re.DOTALL
        )
        assert m, "auth/local block not found"
        block = m.group(1)
        assert "allow 127.0.0.1;" in block
        assert "allow ::1;" in block
        assert "deny all;" in block

    def test_hosted_disabled(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_HOSTED_PORTS_PER_WORKSPACE": "0",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ^~ /hosted/ {" in conf
        assert "return 404;" in conf

    def test_hosted_enabled_default(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf

    def test_trust_outer_proxy(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_TRUST_OUTER_PROXY": "1"}
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # When trusting outer proxy, X-Forwarded-* come from client headers.
        assert "http_x_forwarded_proto" in conf
        assert "http_x_forwarded_host" in conf

    def test_no_trust_outer_proxy(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # Default: X-Forwarded-* derived from trusted values.
        assert "proxy_set_header X-Forwarded-Proto $scheme;" in conf
        assert "proxy_set_header X-Forwarded-Host $http_host;" in conf

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values resolve in the renderer."""
        secret = tmp_path / "llm.key"
        secret.write_text("file-based-key\n")
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": f"file:{secret}",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer file-based-key"' in conf


class TestHeadlessTemplate:
    """``KLANGK_PORT`` unset ⇒ headless template (#1542).

    Headless emits a single container-egress listener on
    ``KLANGK_EGRESS_PORT`` serving the container→backend paths
    (``/llm-proxy``, ``/api/v1/browser-delegate``,
    ``/api/v1/workspaces/post-chat-message`` + their ``auth_request`` gate).
    No browser listener, no UI, no ``/hosted/``, no ``/auth/local``.
    Setting ``KLANGK_PORT`` ⇒ full template; ``KLANGK_AUTH_MODES`` never
    changes which template renders.
    """

    def test_headless_emits_egress_with_llm(self):
        """Headless + LLM ⇒ /llm-proxy + egress paths, no browser surface."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        # /llm-proxy container-egress location is present, token-gated.
        assert "location ~ ^/llm-proxy/" in conf
        assert "auth_request /api/v1/auth/verify-workspace-token;" in conf
        # The auth_request subrequest target + 401 page ride along.
        assert "location = /api/v1/auth/verify-workspace-token" in conf
        assert "location @token_auth_failed" in conf
        # The other egress locations are served too.
        assert "/api/v1/browser-delegate" in conf
        assert "post-chat-message" in conf
        # UDS upstream lands in the proxied locations.
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf
        # No browser surface whatsoever.
        assert "location / {" not in conf  # no catch-all
        assert "/api/v1/auth/local" not in conf
        assert "/hosted/" not in conf  # no hosted/static UI

    def test_headless_no_llm_still_serves_egress(self):
        """Headless + no LLM ⇒ no /llm-proxy, but browser-delegate /
        post-chat-message + their auth_request infra remain."""
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "location ~ ^/llm-proxy/" not in conf
        # The other egress locations persist (they don't depend on LLM).
        assert "/api/v1/browser-delegate" in conf
        assert "post-chat-message" in conf
        assert "verify-workspace-token" in conf
        assert "@token_auth_failed" in conf
        # Still a valid server block with the container-egress listener.
        assert "listen 8995;" in conf
        assert "daemon off;" in conf

    def test_headless_single_container_egress_listener(self):
        """No browser listener: exactly one listen (container-egress), no
        browser catch-all location."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        # Exactly one listen directive — the container-egress port.
        assert conf.count("\n    listen ") == 1
        assert "listen 8995;" in conf
        # No browser catch-all is served off it.
        assert "location / {" not in conf

    def test_port_set_emits_full_template(self):
        """Regression guard: KLANGK_PORT set ⇒ full browser template."""
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LISTEN": "127.0.0.1",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location / {" in conf
        assert "/api/v1/auth/local" in conf
        # Two listeners: browser (listen {listen}:{port}) + egress.
        assert "listen 127.0.0.1:8997;" in conf
        assert "listen 8995;" in conf

    def test_template_keys_off_port_not_auth(self):
        """AUTH value does not change which template is rendered: unset PORT
        ⇒ headless and set PORT ⇒ full across auth values."""
        for auth in ("none", "password", "both"):
            s_headless = make_settings(
                env={
                    "KLANGK_AUTH_MODES": auth,
                    "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                    "KLANGK_EGRESS_PORT": "8995",
                }
            )
            headless = _renderer(s_headless).render_config(
                uds_upstream("/tmp/klangk.sock")
            )
            assert "location / {" not in headless
            assert "location ~ ^/llm-proxy/" in headless

            s_full = make_settings(
                env={
                    "KLANGK_PORT": "8997",
                    "KLANGK_AUTH_MODES": auth,
                    "KLANGK_EGRESS_PORT": "8995",
                }
            )
            full = _renderer(s_full).render_config(
                tcp_upstream("127.0.0.1", "8997")
            )
            assert "location / {" in full


class TestFindNginxBin:
    def test_configured(self):
        s = make_settings({"KLANGK_NGINX_BIN": "/custom/nginx"})
        assert _renderer(s).find_nginx_bin() == "/custom/nginx"

    def test_fallback_to_which(self):
        s = make_settings({})
        result = _renderer(s).find_nginx_bin()
        # Either found on PATH or falls back to /usr/sbin/nginx.
        assert len(result) > 0

    def test_fallback_to_usr_sbin(self, monkeypatch):
        """When shutil.which returns None, fall back to /usr/sbin/nginx."""
        import klangk_backend.nginx as nginx_mod

        s = make_settings({})
        monkeypatch.setattr(nginx_mod.shutil, "which", lambda _: None)
        assert _renderer(s).find_nginx_bin() == "/usr/sbin/nginx"


class TestDetectHostIPv4s:
    def test_subprocess_failure_returns_empty(self, monkeypatch):
        """When the ip command fails, returns [] (caller uses fallback)."""
        import klangk_backend.nginx as nginx_mod

        def _raise(*a, **kw):
            raise FileNotFoundError("no ip")

        monkeypatch.setattr(nginx_mod.subprocess, "check_output", _raise)
        assert detect_host_ipv4s() == []


class TestDnsResolversFromResolvConf:
    def test_parses_resolv_conf(self, monkeypatch):
        """When KLANGK_DNS_SERVERS is unset, nameservers come from resolv.conf."""
        import klangk_backend.nginx as nginx_mod

        s = make_settings({})
        content = "nameserver 1.1.1.1\nnameserver ::1\n"
        monkeypatch.setattr(
            nginx_mod.Path,
            "read_text",
            lambda self: content,
        )
        result = _renderer(s).detect_dns_resolvers()
        assert "1.1.1.1" in result
        assert "[::1]" in result

    def test_resolv_conf_read_error(self, monkeypatch):
        """OSError reading resolv.conf -> fall back to 8.8.8.8."""
        import klangk_backend.nginx as nginx_mod

        s = make_settings({})

        def _raise(self):
            raise OSError("no resolv.conf")

        monkeypatch.setattr(nginx_mod.Path, "read_text", _raise)
        assert _renderer(s).detect_dns_resolvers() == "8.8.8.8"


class TestContainerAclFallback:
    def test_fallback_when_no_host_ips(self, monkeypatch):
        """When auto-detect yields nothing, fallback RFC1918 ranges are used."""
        import klangk_backend.nginx as nginx_mod

        s = make_settings({})
        monkeypatch.setattr(nginx_mod, "detect_host_ipv4s", lambda: [])
        acl, deny = _renderer(s).compute_container_acls()
        assert "allow 172.16.0.0/12;" in acl
        assert "allow 10.0.0.0/8;" in acl
        assert "deny 172.16.0.0/12;" in deny
        assert "allow all;" in deny


class TestWriteConfig:
    def test_writes_file(self, tmp_path):
        s = make_settings({"KLANGK_EGRESS_PORT": "8995"})
        r = _renderer(s)
        conf_path = tmp_path / "nginx.conf"
        text = r.render_config(tcp_upstream("127.0.0.1", "8997"))
        written = r.write_config(tcp_upstream("127.0.0.1", "8997"), conf_path)
        assert conf_path.read_text() == text
        assert written == text


# ---------------------------------------------------------------------------
# klangkd helpers + watchdog no-op paths (#1396)
# ---------------------------------------------------------------------------


class TestKlangkdHelpers:
    def test_state_dir_required_when_unset(self):
        # #1459/#1461: state_dir has no default — missing fails at construction.
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(env={"KLANGK_DATA_DIR": "/tmp/data"})
        assert "KLANGK_STATE_DIR" in str(exc_info.value)

    def test_state_dir_env_sets_value(self):
        s = make_settings({"KLANGK_STATE_DIR": "/custom/state"})
        assert s.state_dir == "/custom/state"

    def test_state_dir_config_file_sets_value(self, tmp_path):
        # Config file provides state_dir; not shadowed by a seeded env value
        # (klangkd no longer mutates os.environ, #1459).
        cfg = tmp_path / "config.yaml"
        cfg.write_text('state_dir: "/from/config"\n')
        s = KlangkSettings(
            env={"KLANGK_DATA_DIR": str(tmp_path)},
            config_file=str(cfg),
        )
        assert s.state_dir == "/from/config"


# The watchdog is gated only by the internal _KLANGK_DISABLE_NGINX kill
# switch (test-only); nginx is owned unconditionally in real runs. Covered
# below.


class TestWatchdogGate:
    """NginxWatchdog.start() respects the test kill switch; otherwise prepares+spawns."""

    @pytest.mark.asyncio
    async def test_start_noop_when_disabled(self, monkeypatch):
        """No-op when the test-only _KLANGK_DISABLE_NGINX is set."""

        monkeypatch.setenv("_KLANGK_DISABLE_NGINX", "1")
        wd = _wd(make_settings({}))
        await wd.start()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_start_runs_prepare_when_enabled(
        self, monkeypatch, tmp_path
    ):
        """When not disabled, start() runs _prepare then spawns (a stubbed)
        watchdog. The real nginx spawn is e2e-covered; here _watch is stubbed
        so the orchestration (prepare, set _stopping=False, create_task) is
        unit-tested."""
        from klangk_backend.nginx import NginxWatchdog

        sock = str(tmp_path / "klangk.sock")
        s = make_settings(
            env={
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_SOCKET": sock,
                "KLANGK_EGRESS_PORT": "19999",
            }
        )
        monkeypatch.delenv("_KLANGK_DISABLE_NGINX", raising=False)
        monkeypatch.setattr(
            "klangk_backend.nginx.NginxRenderer.find_nginx_bin",
            lambda self: "/fake/nginx",
        )

        spawned = {}

        async def _fake_watch(self_wd, bin_path, conf_path):
            spawned["bin"] = bin_path
            spawned["conf"] = conf_path

        monkeypatch.setattr(NginxWatchdog, "_watch", _fake_watch)
        wd = _wd(s)
        await wd.start()
        try:
            assert wd._task is not None
            assert wd._stopping is False
            assert (tmp_path / "nginx.conf").is_file()
            await wd._task
            assert spawned["bin"] == "/fake/nginx"
            assert spawned["conf"] == str(tmp_path / "nginx.conf")
        finally:
            # UDS mode is now per-Util-instance (_wd builds a fresh one each
            # call), so there's no module global to reset (#1503).
            pass


class TestPrepareNginx:
    """NginxWatchdog._prepare() renders nginx.conf with UDS upstream (#1400)."""

    def test_renders_config_and_returns_paths(self, monkeypatch, tmp_path):

        s = make_settings(
            env={
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_EGRESS_PORT": "19999",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            }
        )
        monkeypatch.setattr(
            "klangk_backend.nginx.NginxRenderer.find_nginx_bin",
            lambda self: "/fake/nginx",
        )
        wd = _wd(s)
        bin_path, conf_path = wd._prepare()
        assert bin_path == "/fake/nginx"
        assert conf_path == str(tmp_path / "nginx.conf")
        assert (tmp_path / "nginx.conf").is_file()
        conf = (tmp_path / "nginx.conf").read_text()
        uds_path = str(tmp_path / "klangk.sock")
        assert f"proxy_pass http://unix:{uds_path}:" in conf


class TestStopWatchdog:
    """NginxWatchdog.stop() teardown when a proc/task were injected."""

    @pytest.mark.asyncio
    async def test_stops_no_proc_no_task(self):
        """Nothing spawned: just clears state (the no-op path)."""

        wd = _wd(make_settings({}))
        await wd.stop()
        assert wd._proc is None
        assert wd._task is None
        assert wd._stopping is True

    @pytest.mark.asyncio
    async def test_stops_terminates_running_proc(self, monkeypatch):
        """A still-running nginx proc is killed via os.killpg (SIGTERM)."""
        import signal

        killpg_calls = []
        monkeypatch.setattr(
            "os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
        )

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                pass  # pragma: no cover

            def kill(self):
                pass  # pragma: no cover

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert killpg_calls == [(12345, signal.SIGTERM)]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_falls_back_to_terminate(self, monkeypatch):
        """Falls back to proc.terminate() when killpg raises."""

        terminated = []
        monkeypatch.setattr("os.killpg", Mock(side_effect=ProcessLookupError))

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                terminated.append(True)

            def kill(self):
                pass  # pragma: no cover

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert terminated == [True]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_cancels_task(self):
        """A watchdog task is cancelled and awaited. Covers the task branch."""

        async def _long_running():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        wd = _wd(make_settings({}))
        wd._task = asyncio.create_task(_long_running())
        await wd.stop()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_stops_kills_on_timeout(self, monkeypatch):
        """If the proc doesn't exit within the timeout, SIGKILL via killpg."""
        import signal

        import klangk_backend.nginx as nginx_mod

        actions = []

        def fake_killpg(pgid, sig):
            actions.append(("killpg", sig))

        monkeypatch.setattr("os.killpg", fake_killpg)

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                actions.append("terminate")  # pragma: no cover

            def kill(self):
                actions.append("kill")  # pragma: no cover

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(nginx_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [
            ("killpg", signal.SIGTERM),
            ("killpg", signal.SIGKILL),
        ]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_kills_fallback_on_timeout(self, monkeypatch):
        """Falls back to proc.kill() when killpg SIGKILL raises."""
        import signal

        import klangk_backend.nginx as nginx_mod

        actions = []
        call_count = [0]

        def fake_killpg(pgid, sig):
            call_count[0] += 1
            if call_count[0] == 1:
                actions.append(("killpg", sig))
            else:
                raise ProcessLookupError

        monkeypatch.setattr("os.killpg", fake_killpg)

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                actions.append("terminate")  # pragma: no cover

            def kill(self):
                actions.append("kill")

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(nginx_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [("killpg", signal.SIGTERM), "kill"]
        assert wd._proc is None
