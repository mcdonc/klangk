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
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "git-credential"
    / "tools"
    / "git-credential-klangk"
)


@pytest.fixture()
def fake_browser_id(tmp_path):
    """Create a fake klangk-browser-id script that prints a test ID."""
    script = tmp_path / "klangk-browser-id"
    script.write_text("#!/bin/sh\necho test-browser-id\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def run_helper(operation, stdin_text="", env_override=None, extra_path=None):
    """Run the credential helper as a subprocess."""
    env = {
        **os.environ,
        "KLANGK_BRIDGE_URL": "",
        "KLANGK_WORKSPACE_TOKEN": "",
    }
    # Remove stale env vars from the old bridge-token era
    env.pop("KLANGK_BRIDGE_TOKEN", None)
    env.pop("KLANGK_BROWSER_ID", None)
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
                "KLANGK_BRIDGE_URL": "http://localhost:9999",
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
                "KLANGK_BRIDGE_URL": "http://localhost:9999",
            },
            extra_path=str(fake_browser_id),
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
    def test_returns_credentials(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        _BridgeHandler.response_body = json.dumps(
            {"username": "octocat", "password": "ghp_abc123"}
        ).encode()

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )

        req = _BridgeHandler.requests[-1]
        assert req["browser_id"] == "test-browser-id"

    def test_unwraps_bridge_result(self, bridge_server, fake_browser_id):
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1

    def test_exits_1_on_unreachable_bridge(self, fake_browser_id):
        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": "http://127.0.0.1:1",
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
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
        try:
            run_helper(
                "get",
                "protocol=https\nhost=github.com\n\n",
                env_override={
                    "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
                    "KLANGK_WORKSPACE_TOKEN": "ws-jwt-123",
                },
                extra_path=str(fake_browser_id),
            )
        finally:
            _BridgeHandler.do_POST = orig_do_post

        assert headers_seen[-1] == "Bearer ws-jwt-123"


class TestStoreAndErase:
    def test_store_forwards_credentials(self, bridge_server, fake_browser_id):
        server, port = bridge_server

        result = run_helper(
            "store",
            "protocol=https\nhost=github.com\nusername=u\npassword=p\n\n",
            env_override={
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
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
                "KLANGK_BRIDGE_URL": f"http://127.0.0.1:{port}",
            },
            extra_path=str(fake_browser_id),
        )
        # store/erase are best-effort
        assert result.returncode == 0
