"""Unit tests for the nginx config renderer (#1396).

These exercise the pure rendering logic (config generation) without running a
real nginx — the runtime ACL enforcement is covered by the e2e suite
(``test_nginx_acl_e2e.py``).
"""

import asyncio
import os

import pytest

from klangk_backend.settings import _invalidate_cache
from klangk_backend.nginx import (
    compute_client_max_body_size,
    compute_container_acls,
    detect_dns_resolvers,
    detect_host_ipv4s,
    find_nginx_bin,
    render_config,
    tcp_upstream,
    uds_upstream,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Snapshot all KLANGK_* env vars and restore them after each test.

    Tests here mutate env via the ``_set()`` helper (which writes
    ``os.environ`` directly, not through monkeypatch), so a plain
    ``monkeypatch.delenv`` at setup wouldn't undo those writes — they'd leak
    into sibling test modules sharing the worker process under xdist (e.g.
    ``test_hosted_disabled`` setting ``KLANGK_HOSTED_PORTS_PER_WORKSPACE=0``
    would make ``test_workspaces`` allocate zero ports). Snapshot the whole
    ``KLANGK_*`` block and restore it verbatim on teardown.
    """
    snapshot = {
        k: os.environ[k] for k in list(os.environ) if k.startswith("KLANGK_")
    }
    for k in list(os.environ):
        if k.startswith("KLANGK_"):
            monkeypatch.delenv(k, raising=False)
    _invalidate_cache()
    yield
    # Restore: clear any KLANGK_* added by this test, then reinstate snapshot.
    for k in [k for k in list(os.environ) if k.startswith("KLANGK_")]:
        os.environ.pop(k, None)
    os.environ.update(snapshot)
    _invalidate_cache()
    _invalidate_cache()


def _set(**kw):
    """Set KLANGK_* env vars + invalidate cache."""
    for k, v in kw.items():
        os.environ["KLANGK_" + k.upper()] = str(v)
    _invalidate_cache()


class TestUpstreams:
    def test_tcp_upstream(self):
        assert tcp_upstream("127.0.0.1", "8997") == "http://127.0.0.1:8997"

    def test_uds_upstream(self):
        assert uds_upstream("/tmp/sock") == "http://unix:/tmp/sock:"


class TestClientMaxBodySize:
    def test_default_500mb(self):
        _set()
        assert compute_client_max_body_size() == "500m"

    def test_custom(self):
        _set(file_upload_size_max="10485760")  # 10 MB
        assert compute_client_max_body_size() == "10m"

    def test_minimum_1m(self):
        _set(file_upload_size_max="100")  # 100 bytes
        assert compute_client_max_body_size() == "1m"

    def test_garbage_falls_back(self):
        _set(file_upload_size_max="not-a-number")
        assert compute_client_max_body_size() == "500m"


class TestContainerAcls:
    def test_explicit_subnets(self):
        _set(container_subnets="10.89.0.0/24,172.30.0.0/16")
        acl, deny = compute_container_acls()
        assert "allow 10.89.0.0/24;" in acl
        assert "allow 172.30.0.0/16;" in acl
        assert "deny all;" in acl
        # 127.0.0.1 NOT implicitly added on explicit override.
        assert "allow 127.0.0.1;" not in acl
        # Deny block denies the non-loopback subnets.
        assert "deny 10.89.0.0/24;" in deny
        assert "allow all;" in deny

    def test_loopback_excluded_from_deny(self):
        _set(container_subnets="127.0.0.1,10.89.0.0/24")
        _, deny = compute_container_acls()
        assert "deny 10.89.0.0/24;" in deny
        assert "deny 127.0.0.1;" not in deny
        assert "allow all;" in deny

    def test_all_loopback_warns(self, caplog):
        _set(container_subnets="127.0.0.1")
        _, deny = compute_container_acls()
        assert "allow all;" in deny
        assert "no non-loopback" in caplog.text

    def test_auto_detect_or_fallback(self):
        """Without explicit subnets: either host IPs or fallback RFC1918."""
        _set()
        acl, deny = compute_container_acls()
        # Must produce *something* (host IPs or fallback).
        assert "deny all;" in acl
        assert "allow all;" in deny


class TestDnsResolvers:
    def test_explicit_servers(self):
        _set(dns_servers="1.2.3.4,5.6.7.8")
        result = detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_ipv6_bracketed(self):
        _set(dns_servers="::1")
        assert "[::1]" in detect_dns_resolvers()

    def test_empty_tokens_skipped(self):
        """Trailing commas / empty entries in KLANGK_DNS_SERVERS are skipped."""
        _set(dns_servers="1.2.3.4,,5.6.7.8,")
        result = detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_fallback(self):
        _set()
        result = detect_dns_resolvers()
        assert len(result) > 0  # from resolv.conf or 8.8.8.8


class TestRenderConfig:
    def test_basic_structure(self):
        _set(nginx_port="8995")
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "daemon off;" in conf
        assert "listen 8995;" in conf
        assert "proxy_pass http://127.0.0.1:8997" in conf
        # Core locations present.
        assert "location /api/v1/browser-delegate" in conf
        assert "location = /api/v1/auth/local" in conf
        assert "location /" in conf

    def test_uds_upstream_in_conf(self):
        _set(nginx_port="8995")
        conf = render_config(uds_upstream("/tmp/klangk.sock"))
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf

    def test_no_llm_block_without_url(self):
        _set()
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" not in conf

    def test_llm_block_with_url(self):
        _set(llm_base_url="http://127.0.0.1:11434")
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" in conf

    def test_llm_api_key_resolved(self):
        _set(
            llm_base_url="http://127.0.0.1:11434",
            llm_api_key="cmd:printf %s resolved-key",
        )
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer resolved-key"' in conf
        assert "cmd:" not in conf

    def test_auth_local_loopback_acl(self):
        _set()
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
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
        _set(hosted_ports_per_workspace="0")
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ^~ /hosted/ {" in conf
        assert "return 404;" in conf

    def test_hosted_enabled_default(self):
        _set()
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf

    def test_trust_outer_proxy(self):
        _set(trust_outer_proxy="1")
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        # When trusting outer proxy, X-Forwarded-* come from client headers.
        assert "http_x_forwarded_proto" in conf
        assert "http_x_forwarded_host" in conf

    def test_no_trust_outer_proxy(self):
        _set()
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        # Default: X-Forwarded-* derived from trusted values.
        assert "proxy_set_header X-Forwarded-Proto $scheme;" in conf
        assert "proxy_set_header X-Forwarded-Host $http_host;" in conf

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values resolve in the renderer."""
        secret = tmp_path / "llm.key"
        secret.write_text("file-based-key\n")
        _set(
            llm_base_url="http://127.0.0.1:11434",
            llm_api_key=f"file:{secret}",
        )
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer file-based-key"' in conf


class TestMinimalTemplate:
    """Socket LISTEN ⇒ minimal (headless) template (#1398).

    Template selection keys off ``KLANGK_LISTEN``'s shape alone: a socket
    path emits only the container-egress ``/llm-proxy`` location (+ its
    workspace-token ``auth_request`` gate + CONTAINER_ACL); TCP emits the
    full browser template. AUTH does not participate.
    """

    def test_socket_emits_minimal_with_llm(self):
        """Socket + LLM ⇒ only /llm-proxy, no browser surface (#1398)."""
        _set(
            listen="/tmp/klangk.sock",
            llm_base_url="http://127.0.0.1:11434",
            nginx_port="8995",
        )
        conf = render_config(uds_upstream("/tmp/klangk.sock"))
        # /llm-proxy container-egress location is present, token-gated.
        assert "location ~ ^/llm-proxy/" in conf
        assert "auth_request /api/v1/auth/verify-workspace-token;" in conf
        # The auth_request subrequest target + 401 page ride along.
        assert "location = /api/v1/auth/verify-workspace-token" in conf
        assert "location @token_auth_failed" in conf
        # UDS upstream lands in the proxied locations.
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf
        # No browser surface whatsoever.
        assert "location / {" not in conf  # no catch-all
        assert "/api/v1/browser-delegate" not in conf
        assert "/api/v1/auth/local" not in conf
        assert "post-chat-message" not in conf
        assert "/hosted/" not in conf  # no hosted/static UI

    def test_socket_no_llm_emits_listener_only(self):
        """Socket + no LLM ⇒ no /llm-proxy, no auth locations; just listener."""
        _set(listen="/tmp/klangk.sock", nginx_port="8995")
        conf = render_config(uds_upstream("/tmp/klangk.sock"))
        assert "location ~ ^/llm-proxy/" not in conf
        assert "verify-workspace-token" not in conf
        assert "@token_auth_failed" not in conf
        # Still a valid server block with the container-egress listener.
        assert "listen 8995;" in conf
        assert "daemon off;" in conf

    def test_socket_single_container_egress_listener(self):
        """No client-facing TCP: exactly one listen (container-egress), no
        browser catch-all location (#1398 criterion 3)."""
        _set(
            listen="/tmp/klangk.sock",
            llm_base_url="http://127.0.0.1:11434",
            nginx_port="8995",
        )
        conf = render_config(uds_upstream("/tmp/klangk.sock"))
        # Exactly one listen directive — the container-egress nginx_port.
        assert conf.count("\n    listen ") == 1
        assert "listen 8995;" in conf
        # No browser catch-all is served off it.
        assert "location / {" not in conf

    def test_tcp_emits_full_template(self):
        """Regression guard: TCP LISTEN ⇒ full browser template (#1398 #2)."""
        _set(listen="127.0.0.1", nginx_port="8995")
        conf = render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location / {" in conf
        assert "/api/v1/browser-delegate" in conf
        assert "/api/v1/auth/local" in conf
        assert "listen 8995;" in conf

    def test_template_keys_off_listen_not_auth(self):
        """AUTH value does not change which template is rendered (#1398 #4):
        socket ⇒ minimal and TCP ⇒ full across auth values."""
        for auth in ("none", "password", "password,oidc"):
            _set(
                listen="/tmp/klangk.sock",
                auth_modes=auth,
                llm_base_url="http://127.0.0.1:11434",
                nginx_port="8995",
            )
            minimal = render_config(uds_upstream("/tmp/klangk.sock"))
            assert "location / {" not in minimal
            assert "location ~ ^/llm-proxy/" in minimal

            _set(listen="127.0.0.1", auth_modes=auth, nginx_port="8995")
            full = render_config(tcp_upstream("127.0.0.1", "8997"))
            assert "location / {" in full


class TestFindNginxBin:
    def test_configured(self):
        _set(nginx_bin="/custom/nginx")
        assert find_nginx_bin() == "/custom/nginx"

    def test_fallback_to_which(self):
        _set()
        result = find_nginx_bin()
        # Either found on PATH or falls back to /usr/sbin/nginx.
        assert len(result) > 0

    def test_fallback_to_usr_sbin(self, monkeypatch):
        """When shutil.which returns None, fall back to /usr/sbin/nginx."""
        import klangk_backend.nginx as nginx_mod

        _set()
        monkeypatch.setattr(nginx_mod.shutil, "which", lambda _: None)
        assert find_nginx_bin() == "/usr/sbin/nginx"


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

        _set()  # no dns_servers
        content = "nameserver 1.1.1.1\nnameserver ::1\n"
        monkeypatch.setattr(
            nginx_mod.Path,
            "read_text",
            lambda self: content,
        )
        result = detect_dns_resolvers()
        assert "1.1.1.1" in result
        assert "[::1]" in result

    def test_resolv_conf_read_error(self, monkeypatch):
        """OSError reading resolv.conf -> fall back to 8.8.8.8."""
        import klangk_backend.nginx as nginx_mod

        _set()

        def _raise(self):
            raise OSError("no resolv.conf")

        monkeypatch.setattr(nginx_mod.Path, "read_text", _raise)
        assert detect_dns_resolvers() == "8.8.8.8"


class TestContainerAclFallback:
    def test_fallback_when_no_host_ips(self, monkeypatch):
        """When auto-detect yields nothing, fallback RFC1918 ranges are used."""
        import klangk_backend.nginx as nginx_mod

        _set()  # no container_subnets
        monkeypatch.setattr(nginx_mod, "detect_host_ipv4s", lambda: [])
        acl, deny = compute_container_acls()
        assert "allow 172.16.0.0/12;" in acl
        assert "allow 10.0.0.0/8;" in acl
        assert "deny 172.16.0.0/12;" in deny
        assert "allow all;" in deny


class TestWriteConfig:
    def test_writes_file(self, tmp_path):
        _set(nginx_port="8995")
        conf_path = tmp_path / "nginx.conf"
        text = render_config(tcp_upstream("127.0.0.1", "8997"))
        from klangk_backend.nginx import write_config

        written = write_config(tcp_upstream("127.0.0.1", "8997"), conf_path)
        assert conf_path.read_text() == text
        assert written == text


# ---------------------------------------------------------------------------
# klangkd helpers + watchdog no-op paths (#1396)
# ---------------------------------------------------------------------------


class TestKlangkdHelpers:
    def test_default_state_dir_env(self, monkeypatch):
        from klangk_backend.klangkd import _default_state_dir

        monkeypatch.setenv("KLANGK_STATE_DIR", "/custom/state")
        assert _default_state_dir() == "/custom/state"

    def test_default_state_dir_devenv(self, monkeypatch):
        from klangk_backend.klangkd import _default_state_dir

        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)
        monkeypatch.setenv("DEVENV_STATE", "/devenv/state")
        assert _default_state_dir() == "/devenv/state"

    def test_default_state_dir_fallback(self, monkeypatch):
        from klangk_backend.klangkd import _default_state_dir

        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)
        monkeypatch.delenv("DEVENV_STATE", raising=False)
        assert _default_state_dir() == "/tmp/klangk-state"


# The watchdog is gated only by the internal _KLANGK_DISABLE_NGINX kill
# switch (test-only); nginx is owned unconditionally in real runs. Covered
# below.


class TestWatchdogGate:
    """start_nginx_watchdog respects the test kill switch; otherwise prepares+spawns."""

    @pytest.mark.asyncio
    async def test_start_noop_when_disabled(self, monkeypatch):
        """No-op when the test-only _KLANGK_DISABLE_NGINX is set."""
        import klangk_backend.main as main

        monkeypatch.setenv("_KLANGK_DISABLE_NGINX", "1")
        await main.start_nginx_watchdog()
        assert main._nginx_task is None  # killed by the switch

    @pytest.mark.asyncio
    async def test_start_runs_prepare_when_enabled(
        self, monkeypatch, tmp_path
    ):
        """When not disabled, start_nginx_watchdog runs _prepare_nginx then spawns
        (a stubbed) watchdog. The real nginx spawn is e2e-covered; here
        _nginx_watchdog is stubbed so the orchestration (prepare, set
        _nginx_stopping=False, create_task) is unit-tested."""
        import klangk_backend.main as main

        sock = str(tmp_path / "klangk.sock")
        _set(listen=sock, state_dir=str(tmp_path), nginx_port="19999")
        monkeypatch.delenv("_KLANGK_DISABLE_NGINX", raising=False)
        monkeypatch.setattr(
            "klangk_backend.nginx.find_nginx_bin", lambda: "/fake/nginx"
        )

        spawned = {}

        async def _fake_watchdog(bin_path, conf_path):
            spawned["bin"] = bin_path
            spawned["conf"] = conf_path

        monkeypatch.setattr(main, "_nginx_watchdog", _fake_watchdog)
        await main.start_nginx_watchdog()
        try:
            assert main._nginx_task is not None
            assert main._nginx_stopping is False
            # prepare ran: config written + paths passed to the watchdog.
            assert (tmp_path / "nginx.conf").is_file()
            await main._nginx_task  # let the stub complete
            assert spawned["bin"] == "/fake/nginx"
        finally:
            import klangk_backend.util as util

            util.set_uds_mode(False)
            main._nginx_task = None
            main._nginx_proc = None


class TestSocketPath:
    """_socket_path() reads KLANGK_LISTEN (the socket IS the listen value)."""

    def test_returns_listen_value(self):
        import klangk_backend.main as main

        _set(listen="/tmp/klangk.sock")
        assert main._socket_path() == "/tmp/klangk.sock"

    def test_falls_back_to_state_dir_when_listen_unset(self, monkeypatch):
        import klangk_backend.main as main

        # When listen is empty/unset, _socket_path defaults to
        # <state_dir>/klangk.sock rather than an empty string.
        _set(listen="", state_dir="/custom/state")
        assert main._socket_path() == "/custom/state/klangk.sock"

    def test_falls_back_to_tmp_when_both_unset(self, monkeypatch):
        import klangk_backend.main as main

        _set(listen="", state_dir="")
        assert main._socket_path() == "/tmp/klangk-state/klangk.sock"


class TestPrepareNginx:
    """_prepare_nginx renders the config + arms UDS mode (no spawn)."""

    def test_renders_config_and_returns_paths(self, monkeypatch, tmp_path):
        import klangk_backend.main as main

        sock = str(tmp_path / "klangk.sock")
        # Socket LISTEN ⇒ minimal (headless) template (#1398). Set an LLM
        # base URL so the /llm-proxy location (which carries the UDS
        # proxy_pass) is actually rendered; without it the minimal server
        # block serves nothing.
        _set(
            listen=sock,
            state_dir=str(tmp_path),
            nginx_port="19999",
            llm_base_url="http://127.0.0.1:11434",
        )
        # Stub the binary lookup so the test doesn't depend on PATH.
        monkeypatch.setattr(
            "klangk_backend.nginx.find_nginx_bin", lambda: "/fake/nginx"
        )
        bin_path, conf_path = main._prepare_nginx()
        assert bin_path == "/fake/nginx"
        assert conf_path == str(tmp_path / "nginx.conf")
        assert (tmp_path / "nginx.conf").is_file()
        conf = (tmp_path / "nginx.conf").read_text()
        # The UDS upstream lands in the /llm-proxy location + its auth_request
        # subrequest target; the minimal template carries no browser surface.
        assert f"proxy_pass http://unix:{sock}:" in conf
        assert "location ~ ^/llm-proxy/" in conf
        assert "location / {" not in conf
        assert "/api/v1/auth/local" not in conf
        # _UDS_MODE armed.
        import klangk_backend.util as util

        assert util._UDS_MODE is True
        util.set_uds_mode(False)  # reset for other tests


class TestStopWatchdogWithInjectedState:
    """stop_nginx_watchdog teardown when a proc/task were injected."""

    @pytest.mark.asyncio
    async def test_stops_no_proc_no_task(self):
        """Nothing spawned: just clears state (the no-op path)."""
        import klangk_backend.main as main

        main._nginx_proc = None
        main._nginx_task = None
        await main.stop_nginx_watchdog()
        assert main._nginx_proc is None
        assert main._nginx_task is None
        assert main._nginx_stopping is True

    @pytest.mark.asyncio
    async def test_stops_sigterms_running_proc(self, monkeypatch):
        """A still-running nginx proc is SIGTERM'd (its process group), then
        awaited. Covers the proc-kill branch with an injected fake proc."""
        import klangk_backend.main as main

        killed = {"term": [], "kill": []}

        class FakeProc:
            returncode = None  # still running
            pid = 4242

            async def wait(self):
                return 0

        monkeypatch.setattr(main.os, "getpgid", lambda pid: 9999)
        monkeypatch.setattr(
            main.os,
            "killpg",
            lambda pgid, sig: killed[
                "term" if sig == main.signal.SIGTERM else "kill"
            ].append(pgid),
        )
        main._nginx_proc = FakeProc()
        main._nginx_task = None
        await main.stop_nginx_watchdog()
        assert killed["term"] == [9999]  # SIGTERM sent to the group
        assert killed["kill"] == []  # exited before the SIGKILL timeout
        assert main._nginx_proc is None

    @pytest.mark.asyncio
    async def test_stops_cancels_task(self):
        """A watchdog task is cancelled and awaited. Covers the task branch."""
        import klangk_backend.main as main

        main._nginx_proc = None  # no proc branch

        async def _long_running():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        main._nginx_task = asyncio.create_task(_long_running())
        await main.stop_nginx_watchdog()
        assert main._nginx_task is None

    @pytest.mark.asyncio
    async def test_stops_sigkills_on_timeout(self, monkeypatch):
        """If the proc doesn't exit within the timeout, SIGKILL follows."""
        import klangk_backend.main as main

        killed = {"term": [], "kill": []}

        class HungProc:
            returncode = None
            pid = 1111

            async def wait(self):
                await asyncio.sleep(100)  # never exits within the 5s timeout
                return 0

        monkeypatch.setattr(main.os, "getpgid", lambda pid: 7777)
        monkeypatch.setattr(
            main.os,
            "killpg",
            lambda pgid, sig: killed[
                "term" if sig == main.signal.SIGTERM else "kill"
            ].append(pgid),
        )

        async def _fake_wait_for(coro, timeout):
            # Simulate the 5s grace elapsing: close the coro (avoids an
            # un-awaited-coroutine warning) and raise TimeoutError so the
            # SIGKILL branch fires.
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(main.asyncio, "wait_for", _fake_wait_for)
        main._nginx_proc = HungProc()
        main._nginx_task = None
        await main.stop_nginx_watchdog()
        assert killed["term"] == [7777]
        assert killed["kill"] == [7777]  # SIGKILL after timeout
        assert main._nginx_proc is None

    @pytest.mark.asyncio
    async def test_stops_sigterm_missing_group(self, monkeypatch):
        """SIGTERM raises ProcessLookupError (group already gone) — covers the
        outer killpg exception arm."""
        import klangk_backend.main as main

        class Proc:
            returncode = None
            pid = 3333

            async def wait(self):
                return 0

        def _killpg_raises(pgid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(main.os, "getpgid", lambda pid: 9999)
        monkeypatch.setattr(main.os, "killpg", _killpg_raises)
        main._nginx_proc = Proc()
        main._nginx_task = None
        await main.stop_nginx_watchdog()
        assert main._nginx_proc is None

    @pytest.mark.asyncio
    async def test_stops_handles_missing_process_group(self, monkeypatch):
        """Covers the inner SIGKILL ProcessLookupError arm: SIGTERM kills the
        group, but on the SIGKILL-after-timeout path the group is gone."""
        import klangk_backend.main as main

        class ProcThatHangsThenGroupGone:
            returncode = None
            pid = 2222

            async def wait(self):
                await asyncio.sleep(100)  # never exits within the timeout
                return 0

        # SIGTERM succeeds (no raise); the SIGKILL path raises ProcessLookupError
        # (group already reaped) — covers both the successful-term branch and
        # the inner SIGKILL exception arm (lines 510-511).
        term_calls = []

        def _killpg(pgid, sig):
            if sig == main.signal.SIGTERM:
                term_calls.append(pgid)
            else:  # SIGKILL — group already gone
                raise ProcessLookupError()

        monkeypatch.setattr(main.os, "getpgid", lambda pid: 8888)
        monkeypatch.setattr(main.os, "killpg", _killpg)

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(main.asyncio, "wait_for", _fake_wait_for)
        main._nginx_proc = ProcThatHangsThenGroupGone()
        main._nginx_task = None
        await main.stop_nginx_watchdog()
        assert term_calls == [8888]  # SIGTERM sent; SIGKILL arm swallowed
        assert main._nginx_proc is None
