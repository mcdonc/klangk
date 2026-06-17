"""Tests for the git-credential-klangk helper script."""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "containers"
    / "workspace"
    / "git-credential-klangk.py"
)


def run_helper(operation, stdin_text="", env_override=None):
    """Run the credential helper as a subprocess."""
    import os

    env = {
        **os.environ,
        "KLANGK_BRIDGE_URL": "",
        "KLANGK_BRIDGE_TOKEN": "",
        "KLANGK_WORKSPACE_TOKEN": "",
        "KLANGK_BRIDGE_TIMEOUT_SECONDS": "5",
    }
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
    def test_get_exits_1_when_no_bridge_url(self):
        result = run_helper("get", "protocol=https\nhost=github.com\n\n")
        assert result.returncode == 1

    def test_get_exits_1_when_no_bridge_token(self):
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={"KLANGK_BRIDGE_URL": "http://localhost:9999"},
        )
        assert result.returncode == 1

    def test_store_exits_1_when_no_bridge(self):
        result = run_helper("store", "protocol=https\nhost=github.com\n\n")
        assert result.returncode == 1

    def test_unknown_operation_exits_0(self):
        result = run_helper(
            "unknown",
            "",
            env_override={
                "KLANGK_BRIDGE_URL": "http://localhost:9999",
                "KLANGK_BRIDGE_TOKEN": "tok",
            },
        )
        assert result.returncode == 0


class _BridgeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that records requests and returns canned responses."""

    requests = []
    response_body = b"{}"
    response_status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.requests.append(json.loads(body))
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.__class__.response_body)

    def log_message(self, *args):
        pass  # suppress output


@pytest.fixture()
def bridge_server():
    """Start a local HTTP server acting as the bridge."""
    _BridgeHandler.requests = []
    _BridgeHandler.response_body = b"{}"
    _BridgeHandler.response_status = 200

    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port
    server.shutdown()


class TestGetOperation:
    def test_returns_credentials(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "octocat", "password": "ghp_abc123"}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "test-token",
            },
        )

        assert result.returncode == 0
        assert "username=octocat" in result.stdout
        assert "password=ghp_abc123" in result.stdout

    def test_unwraps_bridge_result(self, bridge_server):
        """Bridge wraps plugin response in {"status":"ok","result":"..."}."""
        server, port = bridge_server
        inner = json.dumps({"username": "octocat", "password": "ghp_xyz"})
        _BridgeHandler.response_body = json.dumps(
            {"status": "ok", "result": inner}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "test-token",
            },
        )

        assert result.returncode == 0
        assert "username=octocat" in result.stdout
        assert "password=ghp_xyz" in result.stdout

    def test_exits_1_on_empty_response(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_body = b"{}"

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "test-token",
            },
        )
        assert result.returncode == 1

    def test_exits_1_on_bridge_error(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_status = 500

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "test-token",
            },
        )
        assert result.returncode == 1

    def test_exits_1_on_unreachable_bridge(self):
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": "http://127.0.0.1:1",
                "KLANGK_BRIDGE_TOKEN": "test-token",
                "KLANGK_BRIDGE_TIMEOUT_SECONDS": "1",
            },
        )
        assert result.returncode == 1

    def test_sends_path_when_present(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()

        run_helper(
            "get",
            "protocol=https\nhost=github.com\npath=foo/bar.git\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "tok",
            },
        )

        req = _BridgeHandler.requests[-1]
        assert req["path"] == "foo/bar.git"

    def test_sends_workspace_token_header(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()

        # Capture the Authorization header
        headers_seen = []
        orig_do_post = _BridgeHandler.do_POST

        def capturing_post(self):
            headers_seen.append(self.headers.get("Authorization", ""))
            orig_do_post(self)

        _BridgeHandler.do_POST = capturing_post
        try:
            run_helper(
                "get",
                "protocol=https\nhost=github.com\n\n",
                env_override={
                    "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                    "KLANGK_BRIDGE_TOKEN": "tok",
                    "KLANGK_WORKSPACE_TOKEN": "ws-jwt-123",
                },
            )
        finally:
            _BridgeHandler.do_POST = orig_do_post

        assert headers_seen[-1] == "Bearer ws-jwt-123"


class TestStoreAndErase:
    def test_store_forwards_credentials(self, bridge_server):
        server, port = bridge_server

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\nusername=u\npassword=p\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "tok",
            },
        )

        assert result.returncode == 0
        req = _BridgeHandler.requests[-1]
        assert req["operation"] == "store"
        assert req["username"] == "u"
        assert req["password"] == "p"

    def test_erase_forwards_to_bridge(self, bridge_server):
        server, port = bridge_server

        result = run_helper(
            "erase",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "tok",
            },
        )

        assert result.returncode == 0
        req = _BridgeHandler.requests[-1]
        assert req["operation"] == "erase"

    def test_store_succeeds_on_bridge_error(self, bridge_server):
        server, port = bridge_server
        _BridgeHandler.response_status = 500

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "KLANGK_BRIDGE_TOKEN": "tok",
            },
        )
        # store/erase are best-effort
        assert result.returncode == 0
