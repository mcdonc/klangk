"""Tests for the klangk-jwt helper script (features/klangk-jwt/tools/)."""

import importlib.machinery
import importlib.util
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
    / "klangk-jwt"
    / "tools"
    / "klangk-jwt"
)

# The helper is a script (no .py suffix); load it as a module so the pure
# mechanics (the envelope unwrap) can be unit-tested in-process. main() is
# __main__-guarded.
_loader = importlib.machinery.SourceFileLoader("klangk_jwt_tool", str(SCRIPT))
_spec = importlib.util.spec_from_loader("klangk_jwt_tool", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)


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


class _BridgeHandler(BaseHTTPRequestHandler):
    """Records the POST and serves a canned response body."""

    requests = []
    response_body = b"{}"
    response_status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.__class__.requests.append(
            {
                "json": json.loads(self.rfile.read(length) or b"{}"),
                "authorization": self.headers.get("Authorization", ""),
            }
        )
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


def run_helper(env_override=None, extra_path=None):
    """Run the klangk-jwt helper as a subprocess."""
    env = {
        **os.environ,
        "KLANGKWS_BRIDGE_URL": "",
    }
    if extra_path:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


def _envelope(inner: dict) -> bytes:
    """The bridge's wrapped form: {"status": "ok", "result": "<json>"}."""
    return json.dumps({"status": "ok", "result": json.dumps(inner)}).encode()


class TestNoBridge:
    def test_exits_1_when_no_bridge_url(self, fake_browser_id):
        result = run_helper(extra_path=str(fake_browser_id))
        assert result.returncode == 1
        assert "no bridge configured" in result.stderr
        assert result.stdout == ""


class TestBridge:
    def test_prints_token_on_stdout_and_nothing_else(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = _envelope({"token": "jwt-abc"})

        result = run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 0
        assert result.stdout == "jwt-abc\n"
        assert result.stderr == ""

    def test_sends_action_and_browser_id_in_payload(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = _envelope({"token": "jwt-abc"})

        run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )

        req = _BridgeHandler.requests[-1]
        assert req["json"] == {
            "action": "klangk_jwt",
            "browser_id": "test-browser-id",
        }
        assert req["authorization"] == ""

    def test_sends_workspace_token_as_bearer(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        token_script = fake_browser_id / "klangk-workspace-token"
        token_script.write_text("#!/bin/sh\necho ws-token\n")
        _BridgeHandler.response_body = _envelope({"token": "jwt-abc"})

        run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )

        req = _BridgeHandler.requests[-1]
        assert req["authorization"] == "Bearer ws-token"

    def test_user_denial_reports_cancelled(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = _envelope({"error": "cancelled"})

        result = run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1
        assert "browser declined: cancelled" in result.stderr
        assert result.stdout == ""

    def test_no_token_in_response_exits_1(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_body = _envelope({"status": "ok"})

        result = run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1
        assert "browser returned no token" in result.stderr

    def test_bridge_http_error_exits_1_with_detail(
        self, bridge_server, fake_browser_id
    ):
        server, port = bridge_server
        _BridgeHandler.response_status = 403
        _BridgeHandler.response_body = json.dumps(
            {"detail": "Unknown browser ID"}
        ).encode()

        result = run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}"},
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1
        assert "bridge refused: 403" in result.stderr
        assert "Unknown browser ID" in result.stderr

    def test_unreachable_bridge_exits_1(self, fake_browser_id):
        result = run_helper(
            env_override={"KLANGKWS_BRIDGE_URL": "http://127.0.0.1:1"},
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 1
        assert "bridge unreachable" in result.stderr


class TestUnwrap:
    """The envelope unwrap in isolation (module-level unit tests)."""

    def test_unwraps_a_wrapped_result(self):
        inner = {"token": "jwt-abc"}
        assert (
            helper._unwrap_bridge_response(
                {"status": "ok", "result": json.dumps(inner)}
            )
            == inner
        )

    def test_passthrough_on_unwrapped_response(self):
        assert helper._unwrap_bridge_response({"token": "jwt-abc"}) == {
            "token": "jwt-abc"
        }

    def test_passthrough_on_garbled_result(self):
        resp = {"status": "ok", "result": "not-json"}
        assert helper._unwrap_bridge_response(resp) == resp
