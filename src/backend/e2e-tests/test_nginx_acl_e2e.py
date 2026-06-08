"""
E2E tests for the nginx container ACL (LLM proxy / browser-delegate).

TestNginxAclConfig — runs nginx.sh with controlled env to verify the
generated nginx.conf contains the correct allow/deny directives.

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

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "scripts"
)
NGINX_SH = os.path.join(SCRIPTS_DIR, "nginx.sh")
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")


def _find_free_port():
    import socket

    with socket.socket() as s:
        s.bind(("", 0))
        return str(s.getsockname()[1])


def _run_nginx_sh(env_overrides, tmpdir):
    """Run nginx.sh just far enough to generate nginx.conf, then kill it.

    nginx.sh ends with ``exec nginx ...`` which blocks. We set a short
    alarm so the script generates the config and then we grab it.
    """
    env = {
        "HOME": tmpdir,
        "PATH": os.environ["PATH"],
        "DEVENV_STATE": tmpdir,
        "KLANGK_NGINX_PORT": "19999",
        "KLANGK_PORT": "19998",
        # Provide a fake podman that always fails so we test the fallback.
        "KLANGK_PODMAN_BIN": "/bin/false",
        **env_overrides,
    }
    # We only need the generated config, not a running nginx. Run the
    # script but kill it once the config file appears (exec nginx blocks).
    proc = subprocess.Popen(
        ["bash", NGINX_SH],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    conf_path = os.path.join(tmpdir, "nginx", "nginx.conf")
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(conf_path):
            # Give it a moment to finish writing.
            time.sleep(0.2)
            break
        time.sleep(0.1)
    proc.kill()
    proc.wait(timeout=5)
    if not os.path.exists(conf_path):
        raise RuntimeError(
            f"nginx.conf not generated.\nstderr: {proc.stderr.read().decode()}"
        )
    return open(conf_path).read()


class TestNginxAclConfig:
    """Verify that nginx.sh generates correct allow/deny lines."""

    def test_explicit_subnets(self, tmp_path):
        """KLANGK_CONTAINER_SUBNETS override produces exact allow lines."""
        conf = _run_nginx_sh(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        assert "allow 10.89.0.0/24;" in conf
        assert "allow 172.30.0.0/16;" in conf
        assert "deny all;" in conf
        # Explicit override: 127.0.0.1 is NOT implicitly added.
        assert "allow 127.0.0.1;" not in conf
        # Broad ranges should NOT appear.
        assert "allow 172.16.0.0/12;" not in conf
        assert "allow 10.0.0.0/8;" not in conf
        assert "allow 192.168.0.0/16;" not in conf

    def test_fallback_ranges(self, tmp_path):
        """When detection fails and no override, fallback ranges are used."""
        # Keep bash/jq on PATH but hide docker so the fallback triggers.
        path_dirs = [
            d
            for d in os.environ["PATH"].split(":")
            if not os.path.isfile(os.path.join(d, "docker"))
        ]
        conf = _run_nginx_sh(
            {
                # No KLANGK_CONTAINER_SUBNETS, podman is /bin/false,
                # docker not on PATH → fallback.
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "PATH": ":".join(path_dirs),
            },
            str(tmp_path),
        )
        assert "allow 172.16.0.0/12;" in conf
        assert "allow 10.0.0.0/8;" in conf
        assert "allow 127.0.0.1;" in conf
        assert "deny all;" in conf
        # 192.168 should never appear.
        assert "allow 192.168.0.0/16;" not in conf

    def test_no_llm_block_without_url(self, tmp_path):
        """LLM proxy block is omitted when KLANGK_LLM_BASE_URL is unset."""
        conf = _run_nginx_sh(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"},
            str(tmp_path),
        )
        assert "llm-proxy" not in conf

    def test_llm_block_present_with_url(self, tmp_path):
        """LLM proxy block is included when KLANGK_LLM_BASE_URL is set."""
        conf = _run_nginx_sh(
            {
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            },
            str(tmp_path),
        )
        assert "llm-proxy" in conf
        assert "allow 10.89.0.0/24;" in conf

    def test_browser_delegate_has_acl(self, tmp_path):
        """browser-delegate endpoint always gets the ACL."""
        conf = _run_nginx_sh(
            {"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"},
            str(tmp_path),
        )
        # Find the browser-delegate location block and check it has the ACL.
        bd_match = re.search(
            r"location = /api/browser-delegate \{(.*?)\}",
            conf,
            re.DOTALL,
        )
        assert bd_match, "browser-delegate location block not found"
        bd_block = bd_match.group(1)
        assert "allow 10.89.0.0/24;" in bd_block
        assert "deny all;" in bd_block


class TestNginxAclEnforcement:
    """Start nginx + uvicorn and verify ACL enforcement at runtime."""

    @pytest.fixture(scope="class")
    def nginx_stack(self, tmp_path_factory):
        """Start uvicorn + nginx with a restrictive KLANGK_CONTAINER_SUBNETS.

        KLANGK_CONTAINER_SUBNETS=192.0.2.0/24 (TEST-NET-1). With an
        explicit override, 127.0.0.1 is NOT implicitly added, so
        requests from localhost are denied on ACL-gated endpoints
        (/llm-proxy, /api/browser-delegate). Regular endpoints (/)
        should still work.
        """
        tmpdir = str(tmp_path_factory.mktemp("nginx-acl"))
        data_dir = os.path.join(tmpdir, "data")
        os.makedirs(data_dir)

        backend_port = _find_free_port()
        nginx_port = _find_free_port()

        # Start uvicorn.
        backend_env = {
            **os.environ,
            "KLANGK_PORT": backend_port,
            "KLANGK_DATA_DIR": data_dir,
            "KLANGK_JWT_SECRET": "nginx-acl-test-secret",
            "KLANGK_DEFAULT_USER": "test@example.com",
            "KLANGK_DEFAULT_PASSWORD": "testpass",
            "KLANGK_TEST_MODE": "1",
            "KLANGK_INSTANCE_ID": "nginx-acl-e2e",
            "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
            "KLANGK_PORT_RANGE_START": "9200",
            "LOGFIRE_TOKEN": "",
        }
        backend_proc = subprocess.Popen(
            [
                "uvicorn",
                "klangk_backend.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                backend_port,
                "--ws-max-size",
                "16777216",
            ],
            cwd=BACKEND_DIR,
            env=backend_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for backend.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://localhost:{backend_port}/health", timeout=2
                )
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
        else:
            backend_proc.kill()
            raise RuntimeError("Backend did not start")

        # Start nginx via nginx.sh.
        nginx_env = {
            "HOME": tmpdir,
            "PATH": os.environ["PATH"],
            "DEVENV_STATE": tmpdir,
            "KLANGK_NGINX_PORT": nginx_port,
            "KLANGK_PORT": backend_port,
            # TEST-NET-1: ensures localhost is NOT allowed.
            "KLANGK_CONTAINER_SUBNETS": "192.0.2.0/24",
            # Need a real-ish LLM URL so the proxy block is generated.
            # It won't actually be reached since the ACL denies us.
            "KLANGK_LLM_BASE_URL": f"http://127.0.0.1:{backend_port}",
            "KLANGK_LLM_API_KEY": "fake-key",
            "KLANGK_PODMAN_BIN": "/bin/false",
        }
        nginx_proc = subprocess.Popen(
            ["bash", NGINX_SH],
            env=nginx_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for nginx.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://localhost:{nginx_port}/health", timeout=2
                )
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
        else:
            nginx_proc.kill()
            backend_proc.kill()
            raise RuntimeError("Nginx did not start")

        yield {
            "nginx_port": nginx_port,
            "backend_port": backend_port,
        }

        nginx_proc.kill()
        nginx_proc.wait(timeout=5)
        backend_proc.kill()
        backend_proc.wait(timeout=5)

    def test_regular_endpoint_allowed(self, nginx_stack):
        """Regular endpoints (/) are not ACL-gated and should work."""
        r = httpx.get(
            f"http://127.0.0.1:{nginx_stack['nginx_port']}/health",
            timeout=5,
        )
        assert r.status_code == 200

    def test_llm_proxy_denied(self, nginx_stack):
        """LLM proxy returns 403 when source IP is not in allowed subnet."""
        r = httpx.get(
            f"http://127.0.0.1:{nginx_stack['nginx_port']}/llm-proxy/v1/models",
            timeout=5,
        )
        assert r.status_code == 403

    def test_browser_delegate_denied(self, nginx_stack):
        """browser-delegate returns 403 from non-container IP."""
        r = httpx.post(
            f"http://127.0.0.1:{nginx_stack['nginx_port']}/api/browser-delegate",
            timeout=5,
        )
        assert r.status_code == 403
