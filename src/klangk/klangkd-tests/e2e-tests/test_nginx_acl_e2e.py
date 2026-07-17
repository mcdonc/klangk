"""
E2E tests for the nginx container ACL (LLM proxy / browser-delegate).

TestNginxAclConfig — renders nginx.conf via the Python renderer (#1396) with
controlled env to verify the generated config contains the correct
allow/deny directives (replaces the old scripts/nginx.sh invocation).

TestNginxAclEnforcement — starts nginx + uvicorn and verifies that
requests from 127.0.0.1 are denied when KLANGK_CONTAINER_SUBNETS is
set to a non-local subnet (explicit override does not add 127.0.0.1).
"""

import os
import re
import subprocess
import time

import httpx
import pytest

from klangk.model import free_port
from _e2e_server import start_server, stop_server

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")


def _render_conf(env_overrides, tmpdir=None):
    """Render nginx.conf via the Python renderer (#1396) with controlled env.

    Replaces the old ``_run_nginx_sh`` (which ran ``scripts/nginx.sh`` and
    killed it after config generation). Renders via :func:`klangk.nginx.render_config`
    from an explicit settings dict built from ``env_overrides`` — no
    ``os.environ`` mutation. Returns the conf text.

    Keys the renderer consults but that aren't in ``env_overrides`` are
    absent from the dict (unset) so each test starts from a known-clean
    state — without this, ``test_no_llm_block_*`` would see a
    ``KLANGK_LLM_BASE_URL`` leaked from ``test_llm_block_*``.
    """
    env = {
        "KLANGK_PORT": "19998",
        "KLANGK_LISTEN": "127.0.0.1",
        "KLANGK_EGRESS_PORT": "19999",
        "KLANGK_DATA_DIR": str(tmpdir or "/tmp/klangk-e2e-data"),
        "KLANGK_STATE_DIR": str(tmpdir or "/tmp/klangk-e2e-state"),
        **env_overrides,
    }
    from klangk.nginx import NginxRenderer, tcp_upstream
    from klangk.settings import KlangkSettings
    import types

    return NginxRenderer(
        types.SimpleNamespace(
            state=types.SimpleNamespace(settings=KlangkSettings(env))
        )
    ).render_config(tcp_upstream("127.0.0.1", "19998"))


def _find_free_port():
    return str(free_port())


class TestNginxAclConfig:
    """Verify that nginx.sh generates correct allow/deny lines."""

    def test_explicit_subnets(self, tmp_path):
        """KLANGK_CONTAINER_SUBNETS override produces exact allow lines."""
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        # Scope the CONTAINER_ACL checks to a container-endpoint location
        # block (browser-delegate), not the whole config — the /auth/local
        # block (#1374) legitimately emits its own `allow 127.0.0.1;`, so a
        # whole-config grep would be ambiguous.
        bd = re.search(
            r"location /api/v1/browser-delegate \{(.*?)\}",
            conf,
            re.DOTALL,
        ).group(1)
        assert "allow 10.89.0.0/24;" in bd
        assert "allow 172.30.0.0/16;" in bd
        assert "deny all;" in bd
        # Explicit override: 127.0.0.1 is NOT implicitly added to CONTAINER_ACL.
        assert "allow 127.0.0.1;" not in bd
        # Broad ranges should NOT appear.
        assert "allow 172.16.0.0/12;" not in bd
        assert "allow 10.0.0.0/8;" not in bd
        assert "allow 192.168.0.0/16;" not in bd

    def test_auto_detect_host_ips(self, tmp_path):
        """Without override, host IPv4 addresses are auto-detected."""
        conf = _render_conf(
            {"KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434"},
            str(tmp_path),
        )
        # Scope to the container-endpoint ACL (see test_explicit_subnets for
        # why not whole-config): 127.0.0.1 is always a host IP, so it must be
        # allowed in CONTAINER_ACL.
        bd = re.search(
            r"location /api/v1/browser-delegate \{(.*?)\}",
            conf,
            re.DOTALL,
        ).group(1)
        assert "allow 127.0.0.1;" in bd
        assert "deny all;" in bd
        # Broad RFC1918 ranges should NOT appear (those are fallback only).
        assert "allow 172.16.0.0/12;" not in bd
        assert "allow 10.0.0.0/8;" not in bd
        assert "allow 192.168.0.0/16;" not in bd

    def test_no_llm_block_without_url(self, tmp_path):
        """LLM proxy block is omitted when KLANGK_LLM_BASE_URL is unset."""
        conf = _render_conf(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"},
            str(tmp_path),
        )
        assert "llm-proxy" not in conf

    def test_llm_block_present_with_url(self, tmp_path):
        """LLM proxy block is included when KLANGK_LLM_BASE_URL is set."""
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        assert "llm-proxy" in conf
        assert "allow 10.89.0.0/24;" in conf

    def test_llm_api_key_cmd_prefix_resolved(self, tmp_path):
        """A cmd:-prefixed KLANGK_LLM_API_KEY is resolved (not emitted verbatim).

        nginx.sh consumes KLANGK_LLM_API_KEY via bash expansion, so it must
        run it through klangk-resolve-value — otherwise the generated
        conf would send `Bearer cmd:...` verbatim as the Authorization
        header.
        """
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "cmd:printf %s resolved-key",
            },
            str(tmp_path),
        )
        # The resolved value appears; the literal prefix does not.
        assert 'Authorization "Bearer resolved-key"' in conf
        assert "cmd:" not in conf

    def test_llm_api_key_file_prefix_resolved(self, tmp_path):
        """A file:-prefixed KLANGK_LLM_API_KEY is read from the file."""
        key_file = tmp_path / "llm-key"
        key_file.write_text("from-file-key\n")
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": f"file:{key_file}",
            },
            str(tmp_path),
        )
        assert 'Authorization "Bearer from-file-key"' in conf
        assert "file:" not in conf

    def test_llm_base_url_cmd_prefix_resolved(self, tmp_path):
        """A cmd:-prefixed KLANGK_LLM_BASE_URL is resolved to the real URL."""
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
                "KLANGK_LLM_BASE_URL": "cmd:printf %s http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        assert "llm-proxy" in conf
        # The resolved URL is used; the literal prefix is not.
        assert "cmd:" not in conf

    def test_browser_delegate_has_acl(self, tmp_path):
        """browser-delegate endpoint always gets the ACL."""
        conf = _render_conf(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"},
            str(tmp_path),
        )
        # Find the browser-delegate location block and check it has the ACL.
        # Prefix match (no "=") covers both /api/v1/browser-delegate and
        # /api/v1/browser-delegate/stream.
        bd_match = re.search(
            r"location /api/v1/browser-delegate \{(.*?)\}",
            conf,
            re.DOTALL,
        )
        assert bd_match, "browser-delegate location block not found"
        bd_block = bd_match.group(1)
        assert "allow 10.89.0.0/24;" in bd_block
        assert "deny all;" in bd_block

    # --- /api/v1/auth/local ACL (#1374) ---
    # In `none` mode this endpoint freely issues an admin token, so the nginx
    # `allow 127.0.0.1/::1; deny all` ACL is the control that keeps workspace
    # containers (which appear via pasta NAT as the host's non-loopback IP)
    # from minting one. It is always generated regardless of mode (outside
    # `none` the backend self-defends), so we assert it unconditionally — a
    # future renderer change that silently drops this block would fail
    # here, where before #1374's review there was no test at all.

    def test_auth_local_has_loopback_acl(self, tmp_path):
        """The /auth/local token handout always gets a loopback-only ACL."""
        conf = _render_conf({}, str(tmp_path))
        # Exact-match location (the `=`). Anchor on the opening brace and pull
        # up to the closing brace so we inspect just this block.
        m = re.search(
            r"location = /api/v1/auth/local \{(.*?)\}",
            conf,
            re.DOTALL,
        )
        assert m, "/auth/local location block not found"
        block = m.group(1)
        assert "allow 127.0.0.1;" in block
        assert "allow ::1;" in block
        assert "deny all;" in block
        # And the block must proxy to the backend (not just deny).
        assert "proxy_pass" in block

    def test_auth_local_acl_independent_of_container_subnets(self, tmp_path):
        """The /auth/local ACL is a fixed loopback allowlist — it must NOT be
        widened by KLANGK_CONTAINER_SUBNETS, or a container could reach the
        free-token endpoint."""
        conf = _render_conf(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"}, str(tmp_path)
        )
        m = re.search(
            r"location = /api/v1/auth/local \{(.*?)\}",
            conf,
            re.DOTALL,
        )
        assert m, "/auth/local location block not found"
        block = m.group(1)
        # Container subnet must not be allowed on the free-token endpoint.
        assert "allow 10.89.0.0/24;" not in block
        assert "deny all;" in block

    # --- deny-by-default on the catch-all `location /` (#1376) ---
    # The catch-all denies the container source IPs so a container can
    # reach ONLY the three explicit container endpoints, not the whole
    # /api/v1/* tree. Safety no longer relies on every backend endpoint
    # remembering its Depends(auth).

    def test_catch_all_denies_container_subnets(self, tmp_path):
        """Catch-all `location /` denies the explicit container subnets — via
        the http-scope geo block, keyed on the pre-realip peer (#1546)."""
        conf = _render_conf(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        # The container-source set lives in the geo block (http scope),
        # not as inline `deny` lines on location / anymore.
        assert "geo $realip_remote_addr $container_source {" in conf
        assert "10.89.0.0/24 1;" in conf
        assert "172.30.0.0/16 1;" in conf
        # The catch-all guard references the geo's variable. The guard
        # line is unique in the config, so assert on the whole conf.
        assert "if ($container_source) { return 403; }" in conf

    def test_catch_all_never_denies_loopback(self, tmp_path):
        """Loopback is never flagged by the geo even when it appears in
        KLANGK_CONTAINER_SUBNETS — local browsers connect via loopback and
        must reach the full UI/API."""
        conf = _render_conf(
            {"KLANGK_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"},
            str(tmp_path),
        )
        assert "10.89.0.0/24 1;" in conf
        assert "127.0.0.1 1;" not in conf
        assert "default 0;" in conf

    def test_catch_all_deny_present_when_containers_configured(self, tmp_path):
        """The container-source guard is always present on the catch-all
        whenever non-loopback container subnets are configured — there is
        no way to opt out of the brute-force cap (#1376)."""
        conf = _render_conf(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"}, str(tmp_path)
        )
        assert "geo $realip_remote_addr $container_source {" in conf
        assert "10.89.0.0/24 1;" in conf
        assert "if ($container_source) { return 403; }" in conf


class TestNginxHostedBlock:
    """KLANGK_HOSTED_PORTS_PER_WORKSPACE gates the /hosted/ proxy (#1237)."""

    def test_default_emits_proxy_locations(self, tmp_path):
        """Unset / non-zero: both hosted proxy locations are present."""
        conf = _render_conf({}, str(tmp_path))
        # slash-less WS-aware redirect-or-proxy location
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf
        # trailing-slash app-proxy location
        assert "location ~ ^/hosted/[^/]+/(\\d+)/(.*)" in conf
        # the disable block is NOT present
        assert (
            "return 404"
            not in conf.split("server {")[1].split("browser-delegate")[0]
        )

    def test_explicit_nonzero_emits_proxy_locations(self, tmp_path):
        """An explicit positive cap still emits the proxy locations."""
        conf = _render_conf(
            {"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "3"}, str(tmp_path)
        )
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf

    def test_zero_replaces_proxy_with_404(self, tmp_path):
        """cap=0 collapses the hosted locations to a single 404 location."""
        conf = _render_conf(
            {"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "0"}, str(tmp_path)
        )
        assert "location ^~ /hosted/ {" in conf
        assert "return 404;" in conf
        # Neither proxy location survives.
        assert "?<hosted_port>" not in conf
        assert "location ~ ^/hosted/[^/]+/(\\d+)/(.*)" not in conf

    def test_non_int_does_not_disable(self, tmp_path):
        """Garbage is not '0', so the proxy stays enabled (backend clamps
        to the default 5; nginx only needs the boolean off-switch)."""
        conf = _render_conf(
            {"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "garbage"}, str(tmp_path)
        )
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf
        assert "return 404;" not in conf


class TestNginxAclEnforcement:
    """Start nginx + uvicorn and verify ACL enforcement at runtime."""

    @pytest.fixture(scope="class")
    @staticmethod
    def nginx_stack(tmp_path_factory):
        """Start uvicorn + nginx with a restrictive KLANGK_CONTAINER_SUBNETS.

        KLANGK_CONTAINER_SUBNETS=192.0.2.0/24 (TEST-NET-1). With an
        explicit override, 127.0.0.1 is NOT implicitly added, so
        requests from localhost are denied on ACL-gated endpoints
        (/llm-proxy, /api/v1/browser-delegate). Regular endpoints (/)
        should still work.
        """
        tmpdir = str(tmp_path_factory.mktemp("nginx-acl"))
        data_dir = os.path.join(tmpdir, "data")
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)

        browser_port = _find_free_port()
        egress_port = _find_free_port()

        # Start the real backend (klangkd on its UDS); nginx (started
        # below) proxies to this socket, as in production (#1525).
        server = start_server(
            data_dir=data_dir,
            state_dir=state_dir,
            KLANGK_JWT_SECRET="nginx-acl-test-secret",
            KLANGK_PREVENT_INSECURE_JWT_SECRET="",
            KLANGK_DEFAULT_USER="test@example.com",
            KLANGK_DEFAULT_PASSWORD="testpass",
            KLANGK_TEST_MODE="1",
            KLANGK_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        uds_path = server["uds_path"]

        # Start nginx via the Python renderer (#1396): render the conf
        # from an explicit settings dict, then launch nginx directly with -c.
        from klangk.nginx import NginxRenderer, uds_upstream
        from klangk.settings import KlangkSettings
        import types

        nginx_env = {
            "KLANGK_PORT": browser_port,
            "KLANGK_LISTEN": "0.0.0.0",
            "KLANGK_EGRESS_PORT": egress_port,
            "KLANGK_CONTAINER_SUBNETS": "192.0.2.0/24",
            "KLANGK_LLM_BASE_URL": "http://127.0.0.1:1",
            "KLANGK_LLM_API_KEY": "fake-key",
            "KLANGK_DATA_DIR": data_dir,
            "KLANGK_STATE_DIR": state_dir,
        }
        nginx_state = os.path.join(tmpdir, "nginx")
        os.makedirs(nginx_state, exist_ok=True)
        conf_path = os.path.join(nginx_state, "nginx.conf")
        NginxRenderer(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=KlangkSettings(nginx_env))
            )
        ).write_config(uds_upstream(uds_path), conf_path)
        nginx_proc = subprocess.Popen(
            ["nginx", "-e", "stderr", "-c", conf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for nginx (browser /health on the browser port).
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://localhost:{browser_port}/health", timeout=2
                )
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
        else:
            nginx_proc.kill()
            stop_server(server)
            raise RuntimeError("Nginx did not start")

        yield {
            "browser_port": browser_port,
            "egress_port": egress_port,
        }

        nginx_proc.kill()
        nginx_proc.wait(timeout=5)
        stop_server(server)

    def test_regular_endpoint_allowed(self, nginx_stack):
        """Regular browser endpoints (/health) are not ACL-gated and should work."""
        r = httpx.get(
            f"http://127.0.0.1:{nginx_stack['browser_port']}/health",
            timeout=5,
        )
        assert r.status_code == 200

    def test_llm_proxy_denied(self, nginx_stack):
        """LLM proxy returns 403 when source IP is not in allowed subnet."""
        r = httpx.get(
            f"http://127.0.0.1:{nginx_stack['egress_port']}/llm-proxy/v1/models",
            timeout=5,
        )
        assert r.status_code == 403

    def test_browser_delegate_denied(self, nginx_stack):
        """browser-delegate returns 403 from non-container IP."""
        r = httpx.post(
            f"http://127.0.0.1:{nginx_stack['egress_port']}/api/v1/browser-delegate",
            timeout=5,
        )
        assert r.status_code == 403


def _host_nonloopback_ipv4():
    """A non-loopback IPv4 of this host — the source IP pasta NAT traffic
    appears as (and thus the IP the catch-all denies). Returns None when there
    is no suitable address (some CI sandboxes), in which case the
    deny-by-default runtime tests skip."""
    import subprocess

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
        # Skip loopback (127/8) and link-local (169.254/16).
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return ip
    return None


class TestNginxDenyByDefault:
    """Runtime enforcement of deny-by-default from container source IPs (#1376).

    The catch-all `location /` denies the container source IPs while allowing
    loopback (local browsers) and other IPs (remote browsers). We simulate a
    container source by connecting to nginx via the host's own non-loopback
    IPv4 — exactly the address pasta NAT traffic appears as — and assert the
    catch-all 403s it (capping the API brute-force surface) while the container
    endpoints' own ACLs still let it through to auth_request.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        host_ip = _host_nonloopback_ipv4()
        if not host_ip:
            pytest.skip("no non-loopback IPv4 to simulate a container source")

        tmpdir = str(tmp_path_factory.mktemp("nginx-deny-default"))
        data_dir = os.path.join(tmpdir, "data")
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)
        browser_port = _find_free_port()
        egress_port = _find_free_port()

        # Start the real backend (klangkd on its UDS); nginx (started
        # below) proxies to this socket (#1525).
        server = start_server(
            data_dir=data_dir,
            state_dir=state_dir,
            KLANGK_JWT_SECRET="nginx-deny-test-secret",
            KLANGK_PREVENT_INSECURE_JWT_SECRET="",
            KLANGK_DEFAULT_USER="test@example.com",
            KLANGK_DEFAULT_PASSWORD="testpass",
            KLANGK_TEST_MODE="1",
            KLANGK_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        uds_path = server["uds_path"]

        # Start nginx via the Python renderer (#1396) with the host IP as
        # the (sole) container source IP. The browser catch-all's container
        # guard (geo on $realip_remote_addr) then denies a *direct* request
        # from exactly that IP — while a trusted-proxy request whose XFF is
        # that IP still passes (the peer is the proxy, not a container source).
        from klangk.nginx import NginxRenderer, uds_upstream
        from klangk.settings import KlangkSettings
        import types

        nginx_env = {
            "KLANGK_PORT": browser_port,
            "KLANGK_LISTEN": "0.0.0.0",
            "KLANGK_EGRESS_PORT": egress_port,
            "KLANGK_CONTAINER_SUBNETS": host_ip,
            "KLANGK_DATA_DIR": data_dir,
            "KLANGK_STATE_DIR": state_dir,
        }
        nginx_state = os.path.join(tmpdir, "nginx")
        os.makedirs(nginx_state, exist_ok=True)
        conf_path = os.path.join(nginx_state, "nginx.conf")
        NginxRenderer(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=KlangkSettings(nginx_env))
            )
        ).write_config(uds_upstream(uds_path), conf_path)
        nginx_proc = subprocess.Popen(
            ["nginx", "-e", "stderr", "-c", conf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for nginx (probe via loopback on the browser port, always allowed).
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{browser_port}/health", timeout=2
                )
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
        else:
            nginx_proc.kill()
            stop_server(server)
            raise RuntimeError("Nginx did not start")

        yield {
            "browser_port": browser_port,
            "egress_port": egress_port,
            "host_ip": host_ip,
        }

        nginx_proc.kill()
        nginx_proc.wait(timeout=5)
        stop_server(server)

    def test_api_denied_from_container_ip(self, stack):
        """From the container source IP, a non-container /api/v1 path is
        refused at nginx (403) — deny-by-default caps the brute-force surface."""
        r = httpx.get(
            f"http://{stack['host_ip']}:{stack['browser_port']}/api/v1/users",
            timeout=5,
        )
        assert r.status_code == 403

    def test_proxied_request_with_container_xff_is_allowed(self, stack):
        """A request whose *immediate* peer is trusted (loopback) but whose
        ``X-Forwarded-For`` is a container-source IP must reach the backend
        (NOT 403). This is the #1546 fix: the container guard keys on the
        pre-realip peer (``$realip_remote_addr``), not the realip-rewritten
        ``$remote_addr`` — so a trusted proxy forwarding a host/container IP
        as the real client is not denied. Without the fix this would 403.
        """
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/users",
            headers={"X-Forwarded-For": stack["host_ip"]},
            timeout=5,
        )
        # Reached the backend (not nginx-denied): 401 unauth is fine.
        assert r.status_code != 403

    def test_api_allowed_from_loopback(self, stack):
        """From loopback, the same /api/v1 path reaches the backend (not 403) —
        local browsers keep full access."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/users",
            timeout=5,
        )
        # Not nginx-denied (401 unauth or similar is fine) — proves loopback
        # is exempt from the catch-all deny.
        assert r.status_code != 403

    def test_health_from_loopback(self, stack):
        """Loopback browser traffic still reaches the app."""
        r = httpx.get(
            f"http://127.0.0.1:{stack['browser_port']}/health", timeout=5
        )
        assert r.status_code == 200

    def test_container_endpoint_acl_still_allows_container_ip(self, stack):
        """The container endpoints keep their own allowlist: from the container
        IP, browser-delegate passes CONTAINER_ACL (reaches auth_request) and
        returns 401, NOT 403 — proving the container IP is not globally blocked,
        only the catch-all."""
        r = httpx.post(
            f"http://{stack['host_ip']}:{stack['egress_port']}/api/v1/browser-delegate",
            timeout=5,
        )
        assert r.status_code == 401
        assert r.status_code != 403


class TestNginxAuthLocalAcl:
    """Runtime enforcement of the /api/v1/auth/local loopback ACL (#1374).

    In `none` mode this endpoint freely issues an admin token, so the nginx
    `allow 127.0.0.1/::1; deny all` ACL is the control that keeps a workspace
    container (which appears via pasta NAT as the host's non-loopback IP) from
    minting one. This is the runtime complement to the config-gen tests in
    TestNginxAclConfig.test_auth_local_* — it proves the generated ACL actually
    fires at request time, not just that the text is present in nginx.conf.

    Two layers are exercised:
      * nginx ACL:   a non-loopback source -> 403 at nginx (never proxied).
      * backend:     a loopback source -> 200 (reaches local_login, which has
                     its own source-IP self-check; see test_api TestLocalLogin).
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(tmp_path_factory):
        host_ip = _host_nonloopback_ipv4()
        if not host_ip:
            pytest.skip("no non-loopback IPv4 to simulate a container source")

        tmpdir = str(tmp_path_factory.mktemp("nginx-auth-local"))
        data_dir = os.path.join(tmpdir, "data")
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)
        browser_port = _find_free_port()
        egress_port = _find_free_port()

        # Start the real backend (klangkd on its UDS); nginx (started
        # below) proxies to this socket (#1525).
        # KLANGK_AUTH_MODES=none so /auth/local actually mints a token.
        server = start_server(
            data_dir=data_dir,
            state_dir=state_dir,
            KLANGK_JWT_SECRET="nginx-auth-local-test-secret",
            KLANGK_PREVENT_INSECURE_JWT_SECRET="",
            KLANGK_DEFAULT_USER="test@example.com",
            KLANGK_DEFAULT_PASSWORD="testpass",
            KLANGK_AUTH_MODES="none",
            KLANGK_TEST_MODE="1",
            KLANGK_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        uds_path = server["uds_path"]

        # nginx via the Python renderer (#1396) with no container subnets —
        # the /auth/local block is always generated with its fixed loopback
        # allowlist.
        from klangk.nginx import NginxRenderer, uds_upstream
        from klangk.settings import KlangkSettings
        import types

        nginx_env = {
            "KLANGK_PORT": browser_port,
            "KLANGK_LISTEN": "0.0.0.0",
            "KLANGK_EGRESS_PORT": egress_port,
            "KLANGK_DATA_DIR": data_dir,
            "KLANGK_STATE_DIR": state_dir,
        }
        nginx_state = os.path.join(tmpdir, "nginx")
        os.makedirs(nginx_state, exist_ok=True)
        conf_path = os.path.join(nginx_state, "nginx.conf")
        NginxRenderer(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=KlangkSettings(nginx_env))
            )
        ).write_config(uds_upstream(uds_path), conf_path)
        nginx_proc = subprocess.Popen(
            ["nginx", "-e", "stderr", "-c", conf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{browser_port}/health", timeout=2
                )
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
        else:
            nginx_proc.kill()
            stop_server(server)
            raise RuntimeError("Nginx did not start")

        yield {
            "browser_port": browser_port,
            "host_ip": host_ip,
        }

        nginx_proc.kill()
        nginx_proc.wait(timeout=5)
        stop_server(server)

    def test_auth_local_denied_from_non_loopback(self, stack):
        """From the host's non-loopback IP (the address pasta NAT traffic
        appears as), POST /auth/local is refused at nginx (403) — the
        free-token endpoint is unreachable to workspace containers."""
        r = httpx.post(
            f"http://{stack['host_ip']}:{stack['browser_port']}/api/v1/auth/local",
            timeout=5,
        )
        assert r.status_code == 403

    def test_auth_local_allowed_from_loopback(self, stack):
        """From loopback (the operator's browser), POST /auth/local reaches
        the backend and mints a token (200) — the auto-login path works."""
        r = httpx.post(
            f"http://127.0.0.1:{stack['browser_port']}/api/v1/auth/local",
            timeout=5,
        )
        assert r.status_code == 200
        assert "access_token" in r.json()
