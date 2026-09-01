"""Tests for the git-credential-klangk helper script."""

import json
import os
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "features"
    / "git-credential"
    / "tools"
    / "git-credential-klangk"
)


@pytest.fixture()
def fake_browser_id(tmp_path):
    """Create fake klangk-browser-id and klangk-workspace-token scripts."""
    script = tmp_path / "klangk-browser-id"
    script.write_text("#!/bin/sh\necho test-browser-id\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    # Default workspace token script returns empty (override per-test)
    token_script = tmp_path / "klangk-workspace-token"
    token_script.write_text("#!/bin/sh\nexit 1\n")
    token_script.chmod(token_script.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def run_helper(operation, stdin_text="", env_override=None, extra_path=None):
    """Run the credential helper as a subprocess."""
    env = {
        **os.environ,
        "KLANGKWS_BRIDGE_URL": "",
    }
    # Remove stale env vars from the old bridge-token era
    env.pop("KLANGKD_BRIDGE_TOKEN", None)
    env.pop("KLANGKWS_BROWSER_ID", None)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    if env_override:
        env.update(env_override)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), operation],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


class TestNoBridge:
    def test_get_exits_1_when_no_bridge_url(self, fake_browser_id):
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1

    def test_get_exits_1_when_no_browser_id(self):
        """No klangk-browser-id on PATH → exits 1."""
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": "http://localhost:9999",
                "PATH": "/nonexistent",
            },
        )
        assert result.returncode == 1

    def test_store_exits_1_when_no_bridge(self):
        result = run_helper("store", "protocol=https\nhost=github.com\n\n")
        assert result.returncode == 1

    def test_unknown_operation_exits_0(self, fake_browser_id):
        result = run_helper(
            "unknown",
            "",
            env_override={
                "KLANGKWS_BRIDGE_URL": "http://localhost:9999",
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 0


class _BridgeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that records requests and returns canned responses.

    Response selection, in order: a path-routed body (``routes`` — used to
    fake GitHub's device-flow endpoints when the helper's GITHUB_URL points
    here), an operation-routed body (``op_bodies`` — keyed on the payload's
    ``operation``), then the catch-all ``response_body``.
    """

    requests = []
    response_body = b"{}"
    response_status = 200
    routes = {}
    op_bodies = {}

    def _response_for(self, path, body_json):
        if path in self.__class__.routes:
            return self.__class__.routes[path]
        op = body_json.get("operation", "")
        if op in self.__class__.op_bodies:
            return self.__class__.op_bodies[op]
        return self.__class__.response_body

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
            self.__class__.requests.append(parsed)
        except (json.JSONDecodeError, ValueError):
            # The faked GitHub endpoints receive form-encoded bodies
            # (the helper posts urlencoded data there); they aren't
            # bridge operations, so don't record them.
            parsed = {}
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._response_for(self.path, parsed))

    def log_message(self, *args):
        pass  # suppress output


@pytest.fixture()
def bridge_server():
    """Start a local HTTP server acting as the bridge."""
    _BridgeHandler.requests = []
    _BridgeHandler.response_body = b"{}"
    _BridgeHandler.response_status = 200
    _BridgeHandler.routes = {}
    _BridgeHandler.op_bodies = {}

    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port
    server.shutdown()
    server.server_close()


class TestGetOperation:
    def test_returns_credentials(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "octocat", "password": "ghp_abc123"}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "username=octocat" in result.stdout
        assert "password=ghp_abc123" in result.stdout

    def test_sends_browser_id_in_payload(self, bridge_server, fake_browser_id):
        """The browser_id from klangk-browser-id is sent in the POST payload."""
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()

        run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        req = _BridgeHandler.requests[-1]
        assert req["browser_id"] == "test-browser-id"

    def test_unwraps_bridge_result(self, bridge_server, fake_browser_id):
        """Bridge wraps feature response in {"status":"ok","result":"..."}."""
        server, port = bridge_server
        inner = json.dumps({"username": "octocat", "password": "ghp_xyz"})
        _BridgeHandler.response_body = json.dumps(
            {"status": "ok", "result": inner}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "username=octocat" in result.stdout
        assert "password=ghp_xyz" in result.stdout

    def test_exits_1_on_empty_response(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        _BridgeHandler.response_body = b"{}"

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1

    def test_exits_1_on_bridge_error(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        _BridgeHandler.response_status = 500

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1

    def test_exits_1_on_unreachable_bridge(self, fake_browser_id):
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": "http://127.0.0.1:1",
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1

    def test_sends_path_when_present(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()

        run_helper(
            "get",
            "protocol=https\nhost=github.com\npath=foo/bar.git\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        req = _BridgeHandler.requests[-1]
        assert req["path"] == "foo/bar.git"

    def test_sends_workspace_token_header(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()

        headers_seen = []
        orig_do_post = _BridgeHandler.do_POST

        def capturing_post(self):
            headers_seen.append(self.headers.get("Authorization", ""))
            orig_do_post(self)

        _BridgeHandler.do_POST = capturing_post
        # Write a fake klangk-workspace-token that returns the test JWT
        token_script = fake_browser_id / "klangk-workspace-token"
        token_script.write_text("#!/bin/sh\necho ws-jwt-123\n")
        token_script.chmod(token_script.stat().st_mode | stat.S_IEXEC)
        try:
            run_helper(
                "get",
                "protocol=https\nhost=github.com\n\n",
                env_override={
                    "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                },
                extra_path=str(fake_browser_id),
            )
        finally:
            _BridgeHandler.do_POST = orig_do_post

        assert headers_seen[-1] == "Bearer ws-jwt-123"


class TestDeviceFlowCache:
    """The device flow must not re-run when the tab cache has a token.

    Regression: with the client ID set, ``get`` used to start a fresh
    device flow on every git operation — git's post-success ``store``
    populated the browser cache, but the helper never consulted it.
    """

    CLIENT_ENV = {"KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "Ov23test"}

    def test_cached_credential_short_circuits_device_flow(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.op_bodies = {
            "peek": json.dumps(
                {"username": "x-access-token", "password": "gho_cached"}
            ).encode()
        }

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                **self.CLIENT_ENV,
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "username=x-access-token" in result.stdout
        assert "password=gho_cached" in result.stdout
        # Only the cache peek reached the bridge — no device_flow_show, no
        # PAT-dialog "get", so no login was attempted.
        assert [r["operation"] for r in _BridgeHandler.requests] == ["peek"]

    def test_device_flow_runs_on_cache_miss(
        self, bridge_server, fake_browser_id
    ):
        """Cache miss → full device flow against the faked GitHub endpoints."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode()
        }
        _BridgeHandler.routes = {
            "/login/device/code": json.dumps(
                {
                    "device_code": "dc-123",
                    "user_code": "ABCD-1234",
                    "verification_uri": f"{base}/login/device",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            "/login/oauth/access_token": json.dumps(
                {"access_token": "gho_fresh", "token_type": "bearer"}
            ).encode(),
        }

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "GIT_CREDENTIAL_KLANGK_GITHUB_URL": base,
                **self.CLIENT_ENV,
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "username=x-access-token" in result.stdout
        assert "password=gho_fresh" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops[0] == "peek"
        assert "device_flow_show" in ops
        assert "device_flow_done" in ops

    def test_peek_error_falls_through_to_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        """A peek the bridge can't answer (error body) must not strand git —
        the helper falls through to the ordinary get (PAT dialog) path."""
        server, port = bridge_server
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"status": "error"}).encode(),
            "get": json.dumps(
                {"username": "octocat", "password": "ghp_pat"}
            ).encode(),
        }

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "GIT_CREDENTIAL_KLANGK_GITHUB_URL": "http://127.0.0.1:1",
                **self.CLIENT_ENV,
            },
            extra_path=str(fake_browser_id),
        )

        # Device flow unreachable (dead GITHUB_URL) → peek error → PAT path.
        assert result.returncode == 0
        assert "username=octocat" in result.stdout
        assert "password=ghp_pat" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops[0] == "peek"
        assert "get" in ops


class TestStoreAndErase:
    def test_store_forwards_credentials(self, bridge_server, fake_browser_id):
        server, port = bridge_server

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\nusername=u\npassword=p\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        req = _BridgeHandler.requests[-1]
        assert req["operation"] == "store"
        assert req["username"] == "u"
        assert req["password"] == "p"

    def test_erase_forwards_to_bridge(self, bridge_server, fake_browser_id):
        server, port = bridge_server

        result = run_helper(
            "erase",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        req = _BridgeHandler.requests[-1]
        assert req["operation"] == "erase"

    def test_store_succeeds_on_bridge_error(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_status = 500

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )
        # store/erase are best-effort
        assert result.returncode == 0


class TestDebugRedaction:
    """Debug output must never leak the password (code-scanning alert 172)."""

    def test_get_does_not_log_bridge_password(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "octocat", "password": "ghp_SUPERSECRET"}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "GIT_CREDENTIAL_KLANGK_DEBUG": "1",
            },
            extra_path=str(fake_browser_id),
        )

        # The credential is still delivered to git via stdout...
        assert "password=ghp_SUPERSECRET" in result.stdout
        # ...but never appears in the debug output on stderr.
        assert "ghp_SUPERSECRET" not in result.stderr
        assert '"password": "***"' in result.stderr

    def test_store_does_not_log_input_password(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\n"
            "username=octocat\npassword=ghp_SUPERSECRET\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "GIT_CREDENTIAL_KLANGK_DEBUG": "1",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "ghp_SUPERSECRET" not in result.stderr
        assert "'password': '***'" in result.stderr
