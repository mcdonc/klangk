"""Tests for the git-credential-klangk helper script."""

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import urllib.parse
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

# The helper is a script (no .py suffix); load it as a module so the pure
# mechanics (PKCE vectors, provider validation, discovery mapping, token
# request shapes) can be unit-tested in-process. Module import reads env
# constants only -- main() is __main__-guarded.
_loader = importlib.machinery.SourceFileLoader(
    "git_credential_klangk", str(SCRIPT)
)
_spec = importlib.util.spec_from_loader("git_credential_klangk", _loader)
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
    fake a provider's device-flow endpoints when a provider entry points
    here), an operation-routed body (``op_bodies`` — keyed on the payload's
    ``operation``), then the catch-all ``response_body``.
    """

    requests = []
    # Form-encoded (non-JSON) bodies — the device-flow endpoint POSTs —
    # parsed into flat dicts so tests can assert client_id/scope.
    forms = []
    response_body = b"{}"
    response_status = 200
    routes = {}
    op_bodies = {}
    # Per-operation callables: op_handlers[op](payload) -> response dict.
    # Needed where the answer depends on the request (the auth flow
    # echoes back the state the helper generated, #3385).
    op_handlers = {}
    # Per-path callables for the faked provider endpoints:
    # route_handlers[path](form) -> response dict, where form is this
    # request's parsed body — for endpoints whose answer depends on the
    # grant_type (a rejected refresh followed by a code exchange).
    route_handlers = {}

    def _response_for(self, path, body_json):
        handler = self.__class__.route_handlers.get(path)
        if handler is not None:
            form = self.__class__.forms[-1] if self.__class__.forms else {}
            return json.dumps(handler(form)).encode()
        if path in self.__class__.routes:
            return self.__class__.routes[path]
        op = body_json.get("operation", "")
        if op in self.__class__.op_handlers:
            return json.dumps(
                self.__class__.op_handlers[op](body_json)
            ).encode()
        if op in self.__class__.op_bodies:
            return self.__class__.op_bodies[op]
        return self.__class__.response_body

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
            self.__class__.requests.append(parsed)
        except json.JSONDecodeError, ValueError:
            # The faked provider endpoints receive form-encoded bodies
            # (the helper posts urlencoded data there); they aren't bridge
            # operations, so record them as parsed forms instead.
            try:
                form = urllib.parse.parse_qs(body.decode())
                self.__class__.forms.append({k: v[0] for k, v in form.items()})
            except UnicodeDecodeError, ValueError:
                pass
            parsed = {}
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._response_for(self.path, parsed))

    def do_GET(self):
        """Serve discovery documents: GET <path> from ``routes``, 404
        otherwise (the helper treats a missing document as 'use the
        entry's flow')."""
        body = self.__class__.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress output


@pytest.fixture()
def bridge_server():
    """Start a local HTTP server acting as the bridge."""
    _BridgeHandler.requests = []
    _BridgeHandler.forms = []
    _BridgeHandler.response_body = b"{}"
    _BridgeHandler.response_status = 200
    _BridgeHandler.routes = {}
    _BridgeHandler.op_bodies = {}
    _BridgeHandler.op_handlers = {}
    _BridgeHandler.route_handlers = {}

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
        # Wrapped in the bridge's {"status": "ok", "result": ...} envelope,
        # exactly as the frontend produces it — the peek path must unwrap
        # (a bare body is a shape production never sends).
        inner = json.dumps(
            {"username": "x-access-token", "password": "gho_cached"}
        )
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"status": "ok", "result": inner}).encode()
        }

        result = run_helper(
            "get",
            "protocol=https\nhost=github.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                # Dead local endpoint: if the short-circuit ever regresses,
                # the device flow fails fast here instead of reaching the
                # real github.com with the fake client id.
                "GIT_CREDENTIAL_KLANGK_GITHUB_URL": "http://127.0.0.1:1",
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


class TestDeviceFlowHostGate:
    """The device-flow gate must normalize the git credential host (#2963).

    Git preserves case and can include an explicit port or trailing dot in
    ``host``; every spelling of a GitHub remote must reach the device flow,
    and non-GitHub hosts must skip it entirely.
    """

    CLIENT_ENV = {"KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "Ov23test"}

    def _run_get(self, bridge_server, fake_browser_id, host):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
            "get": json.dumps({"username": "u", "password": "p"}).encode(),
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
        return run_helper(
            "get",
            f"protocol=https\nhost={host}\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "GIT_CREDENTIAL_KLANGK_GITHUB_URL": base,
                **self.CLIENT_ENV,
            },
            extra_path=str(fake_browser_id),
        )

    @pytest.mark.parametrize(
        "host",
        [
            "github.com",
            "www.github.com",
            "github.com:443",
            "GitHub.com",
            "github.com.",
        ],
    )
    def test_gate_takes_device_flow_for_every_github_spelling(
        self, bridge_server, fake_browser_id, host
    ):
        result = self._run_get(bridge_server, fake_browser_id, host)

        assert result.returncode == 0
        assert "password=gho_fresh" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops[0] == "peek"
        assert "device_flow_show" in ops
        assert "device_flow_done" in ops
        assert "get" not in ops  # never fell through to the PAT dialog

    @pytest.mark.parametrize(
        "host", ["gitlab.com", "github.com.evil.com", "notgithub.com"]
    )
    def test_gate_skips_device_flow_for_non_github_host(
        self, bridge_server, fake_browser_id, host
    ):
        result = self._run_get(bridge_server, fake_browser_id, host)

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]  # no peek, no device flow, straight to PAT


class TestProviderMap:
    """KLANGKWS_FEATURE_OAUTH_PROVIDERS activates the device flow for any
    host (GitLab, Gitea, self-hosted) with per-provider endpoints, scope,
    and username (#432). The poll loop is RFC 8628 standard — only the
    endpoint/credential plumbing is provider-specific.
    """

    def _providers_env(self, base, **overrides):
        """One gitlab.com provider entry pointing at the fake server."""
        entry = {
            "host": "gitlab.com",
            "client_id": "gitlab-id",
            "device_code_url": f"{base}/oauth/authorize_device",
            "token_url": f"{base}/oauth/token",
            "scope": "read_repository write_repository",
            "username": "oauth2",
        }
        entry.update(overrides)
        return json.dumps([entry])

    def _setup_flow(self, base, token="glpat-fresh"):
        """Cache-miss peek + GitLab-style device-flow routes on the fake
        server."""
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
            "get": json.dumps({"username": "u", "password": "p"}).encode(),
        }
        _BridgeHandler.routes = {
            "/oauth/authorize_device": json.dumps(
                {
                    "device_code": "dc-gl",
                    "user_code": "GLCD-1234",
                    "verification_uri": f"{base}/oauth/authorize_device",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            "/oauth/token": json.dumps(
                {"access_token": token, "token_type": "bearer"}
            ).encode(),
        }

    def _run_get(self, bridge_server, fake_browser_id, host, providers_env):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup_flow(base)
        return run_helper(
            "get",
            f"protocol=https\nhost={host}\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": providers_env,
            },
            extra_path=str(fake_browser_id),
        )

    def test_device_flow_runs_for_mapped_host(
        self, bridge_server, fake_browser_id
    ):
        """A gitlab.com push runs the device flow against the provider's
        own endpoints, with the entry's scope and client_id in the code
        request."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            self._providers_env(base),
        )

        assert result.returncode == 0
        assert "username=oauth2" in result.stdout
        assert "password=glpat-fresh" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops[0] == "peek"
        assert "device_flow_show" in ops
        assert "device_flow_done" in ops
        assert "get" not in ops  # never fell through to the PAT dialog
        # The device-code request carried the entry's client_id and scope.
        code_req = _BridgeHandler.forms[0]
        assert code_req["client_id"] == "gitlab-id"
        assert code_req["scope"] == "read_repository write_repository"

    def test_device_flow_show_names_the_provider_host(
        self, bridge_server, fake_browser_id
    ):
        """device_flow_show carries the (normalized) provider host so the
        browser dialog can name the right service."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "GitLab.com:443",
            self._providers_env(base),
        )

        assert result.returncode == 0
        show = next(
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "device_flow_show"
        )
        assert show["host"] == "gitlab.com"
        assert show["verification_uri"] == (f"{base}/oauth/authorize_device")

    def test_custom_username_from_entry(self, bridge_server, fake_browser_id):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            self._providers_env(base, username="x-access-token"),
        )

        assert result.returncode == 0
        assert "username=x-access-token" in result.stdout

    def test_scope_omitted_when_entry_has_none(
        self, bridge_server, fake_browser_id
    ):
        """An empty scope must not be sent as a bare ``scope=`` param —
        providers that reject empty scopes would fail the code request."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            self._providers_env(base, scope=""),
        )

        assert result.returncode == 0
        assert "scope" not in _BridgeHandler.forms[0]

    def test_www_spelling_matches_entry(self, bridge_server, fake_browser_id):
        """A www.gitlab.com remote reaches the gitlab.com entry (the www
        alias mirrors the legacy github.com/www.github.com pair)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "www.gitlab.com",
            self._providers_env(base),
        )

        assert result.returncode == 0
        assert "password=glpat-fresh" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" in ops
        assert "get" not in ops

    def test_map_entry_wins_over_shorthand(
        self, bridge_server, fake_browser_id
    ):
        """Both a github.com map entry and the legacy client-ID shorthand
        set: the explicit map entry takes precedence."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        providers = json.dumps(
            [
                {
                    "host": "github.com",
                    "client_id": "map-id",
                    "device_code_url": f"{base}/login/device/code",
                    "token_url": f"{base}/login/oauth/access_token",
                    "scope": "repo",
                    "username": "x-access-token",
                }
            ]
        )
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
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": providers,
                "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "legacy-id",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "password=gho_fresh" in result.stdout
        assert _BridgeHandler.forms[0]["client_id"] == "map-id"

    def test_shorthand_still_works_alongside_map(
        self, bridge_server, fake_browser_id
    ):
        """A map that doesn't cover github.com leaves the shorthand in
        charge for github.com hosts (backward compatibility)."""
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
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": self._providers_env(base),
                "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "legacy-id",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "password=gho_fresh" in result.stdout
        assert _BridgeHandler.forms[0]["client_id"] == "legacy-id"

    def test_invalid_json_falls_back_to_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        result = self._run_get(
            bridge_server, fake_browser_id, "gitlab.com", "{not json"
        )

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_non_list_json_falls_back_to_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            '"gitlab.com"',
        )

        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_entry_missing_required_fields_skipped(
        self, bridge_server, fake_browser_id
    ):
        """An entry without the required fields is skipped, not fatal —
        the host falls through to the PAT dialog."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        broken = self._providers_env(base)
        broken = broken.replace(f"{base}/oauth/token", "")
        result = self._run_get(
            bridge_server, fake_browser_id, "gitlab.com", broken
        )

        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_unmapped_host_skips_device_flow(
        self, bridge_server, fake_browser_id
    ):
        """bitbucket.org has no provider entry and no shorthand -> PAT
        dialog, no peek."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "bitbucket.org",
            self._providers_env(base),
        )

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_map_host_suffix_boundary_not_matched(
        self, bridge_server, fake_browser_id
    ):
        """A github.com map entry must not match github.com.evil.com --
        exact-key matching after normalization, never suffix matching
        (the map path's version of TestDeviceFlowHostGate)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "github.com.evil.com",
            self._providers_env(base, host="github.com"),
        )

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_broken_map_json_leaves_shorthand_working(
        self, bridge_server, fake_browser_id
    ):
        """A malformed map disables only the map -- the GitHub shorthand
        still flows for github.com."""
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
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": "{not json",
                "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "gh-id",
            },
            extra_path=str(fake_browser_id),
        )

        assert result.returncode == 0
        assert "password=gho_fresh" in result.stdout
        assert _BridgeHandler.forms[0]["client_id"] == "gh-id"


class TestStockShorthands:
    """A bare client ID in a per-provider shorthand env var expands to
    that public instance's stock device-flow entry (GitHub, GitLab).
    Self-hosted instances use the provider map -- a shorthand never
    matches a lookalike host.
    """

    def _setup_flow(self, base, code_path, token_path, token="glpat-fresh"):
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
            "get": json.dumps({"username": "u", "password": "p"}).encode(),
        }
        _BridgeHandler.routes = {
            code_path: json.dumps(
                {
                    "device_code": "dc-x",
                    "user_code": "XLCD-1234",
                    "verification_uri": f"{base}{code_path}",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            token_path: json.dumps(
                {"access_token": token, "token_type": "bearer"}
            ).encode(),
        }

    def _run_get(self, bridge_server, fake_browser_id, host, env):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        return run_helper(
            "get",
            f"protocol=https\nhost={host}\n\n",
            env_override={"KLANGKWS_BRIDGE_URL": base, **env},
            extra_path=str(fake_browser_id),
        )

    def test_gitlab_shorthand_runs_gitlab_flow(
        self, bridge_server, fake_browser_id
    ):
        """The GitLab shorthand hits GitLab's stock endpoints with GitLab's
        scope and username."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup_flow(base, "/oauth/authorize_device", "/oauth/token")
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            {
                "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
                "GIT_CREDENTIAL_KLANGK_GITLAB_URL": base,
            },
        )

        assert result.returncode == 0
        assert "username=oauth2" in result.stdout
        assert "password=glpat-fresh" in result.stdout
        code_req = _BridgeHandler.forms[0]
        assert code_req["client_id"] == "gl-id"
        assert code_req["scope"] == "read_repository write_repository"
        show = next(
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "device_flow_show"
        )
        assert show["host"] == "gitlab.com"

    def test_gitlab_shorthand_matches_www_spelling(
        self, bridge_server, fake_browser_id
    ):
        """www.gitlab.com reaches the gitlab.com shorthand."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup_flow(base, "/oauth/authorize_device", "/oauth/token")
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "www.gitlab.com",
            {
                "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
                "GIT_CREDENTIAL_KLANGK_GITLAB_URL": base,
            },
        )

        assert result.returncode == 0
        assert "password=glpat-fresh" in result.stdout

    def test_shorthand_never_matches_lookalike_host(
        self, bridge_server, fake_browser_id
    ):
        """A self-hosted gitlab.example.com is NOT gitlab.com -- the
        shorthand must not fire (exact host match, never suffix)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup_flow(base, "/oauth/authorize_device", "/oauth/token")
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.example.com",
            {
                "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
                "GIT_CREDENTIAL_KLANGK_GITLAB_URL": "http://127.0.0.1:1",
            },
        )

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops == ["get"]

    def test_shorthands_are_independent(self, bridge_server, fake_browser_id):
        """Both shorthands set: each host reaches its own provider."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        env = {
            "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "gh-id",
            "GIT_CREDENTIAL_KLANGK_GITHUB_URL": base,
            "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
            "GIT_CREDENTIAL_KLANGK_GITLAB_URL": base,
        }
        self._setup_flow(
            base,
            "/login/device/code",
            "/login/oauth/access_token",
            token="gho_fresh",
        )
        github = self._run_get(
            bridge_server, fake_browser_id, "github.com", env
        )
        assert github.returncode == 0
        assert "username=x-access-token" in github.stdout

        self._setup_flow(
            base,
            "/oauth/authorize_device",
            "/oauth/token",
            token="glpat-fresh",
        )
        gitlab = self._run_get(
            bridge_server, fake_browser_id, "gitlab.com", env
        )
        assert gitlab.returncode == 0
        assert "username=oauth2" in gitlab.stdout

    def test_map_wins_over_gitlab_shorthand(
        self, bridge_server, fake_browser_id
    ):
        """An explicit map entry for gitlab.com overrides the shorthand."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        providers = json.dumps(
            [
                {
                    "host": "gitlab.com",
                    "client_id": "map-id",
                    "device_code_url": f"{base}/custom/code",
                    "token_url": f"{base}/custom/token",
                }
            ]
        )
        self._setup_flow(base, "/custom/code", "/custom/token")
        result = self._run_get(
            bridge_server,
            fake_browser_id,
            "gitlab.com",
            {
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": providers,
                "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
                "GIT_CREDENTIAL_KLANGK_GITLAB_URL": "http://127.0.0.1:1",
            },
        )

        assert result.returncode == 0
        assert "password=glpat-fresh" in result.stdout
        assert _BridgeHandler.forms[0]["client_id"] == "map-id"


class TestProviderResponseHardening:
    """A malformed provider response must never crash the helper mid-flow
    (leaving the browser dialog stuck) -- it falls back to the PAT path,
    and the dialog gets a device_flow_error when it was already shown.
    """

    def _setup(self, base, code_body):
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
            "get": json.dumps({"username": "u", "password": "p"}).encode(),
        }
        _BridgeHandler.routes = {
            "/oauth/authorize_device": code_body,
            "/oauth/token": json.dumps(
                {"access_token": "glpat-fresh", "token_type": "bearer"}
            ).encode(),
        }

    def _env(self, base):
        return {
            "KLANGKWS_BRIDGE_URL": base,
            "KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID": "gl-id",
            "GIT_CREDENTIAL_KLANGK_GITLAB_URL": base,
        }

    def _run(self, bridge_server, fake_browser_id, base):
        return run_helper(
            "get",
            "protocol=https\nhost=gitlab.com\n\n",
            env_override=self._env(base),
            extra_path=str(fake_browser_id),
        )

    def test_partial_code_response_falls_back_to_pat(
        self, bridge_server, fake_browser_id
    ):
        """200 with a body missing user_code/verification_uri -> PAT
        dialog, no crash (previously KeyError)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup(base, json.dumps({"device_code": "dc-only"}).encode())
        result = self._run(bridge_server, fake_browser_id, base)

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        # peek, then straight to PAT — nothing was shown
        assert ops == ["peek", "get"]

    def test_non_object_code_response_falls_back_to_pat(
        self, bridge_server, fake_browser_id
    ):
        """200 with a JSON string body -> PAT dialog (previously TypeError
        on the 'device_code' in code_resp check)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup(base, json.dumps("ok").encode())
        result = self._run(bridge_server, fake_browser_id, base)

        assert result.returncode == 0
        assert "password=p" in result.stdout

    def test_string_interval_and_expires_are_coerced(
        self, bridge_server, fake_browser_id
    ):
        """interval/expires_in arriving as JSON strings still flow (no
        TypeError in time.sleep / deadline math)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup(
            base,
            json.dumps(
                {
                    "device_code": "dc-x",
                    "user_code": "XLCD-1234",
                    "verification_uri": f"{base}/oauth/device",
                    "interval": "1",
                    "expires_in": "60",
                }
            ).encode(),
        )
        result = self._run(bridge_server, fake_browser_id, base)

        assert result.returncode == 0
        assert "password=glpat-fresh" in result.stdout

    def test_malformed_token_poll_posts_error_and_falls_back(
        self, bridge_server, fake_browser_id
    ):
        """A token endpoint answering a JSON array crashes the poll loop --
        the helper must post device_flow_error (dismiss the stuck dialog)
        and fall back to the PAT dialog instead of exiting."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        self._setup(
            base,
            json.dumps(
                {
                    "device_code": "dc-x",
                    "user_code": "XLCD-1234",
                    "verification_uri": f"{base}/oauth/device",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
        )
        _BridgeHandler.routes["/oauth/token"] = json.dumps([1, 2]).encode()
        result = self._run(bridge_server, fake_browser_id, base)

        assert result.returncode == 0
        assert "password=p" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" in ops
        error_posts = [
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "device_flow_error"
        ]
        assert error_posts, "poll-loop crash must dismiss the shown dialog"
        assert error_posts[0]["host"] == "gitlab.com"


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


# --- authorization_code + PKCE flow (phase 1 mechanics, #3385) --------


def _pkce_providers_env(base, **overrides):
    """One authorization_code_pkce provider entry pointing at the fake
    server (discovery + endpoints all fake, no live network)."""
    entry = {
        "host": "git.example.com",
        "flow": "authorization_code_pkce",
        "client_id": "cid-e2e",
        "authorize_url": f"{base}/login/oauth/authorize",
        "token_url": f"{base}/login/oauth/access_token",
        "redirect_uri": "http://127.0.0.1:8124/",
    }
    entry.update(overrides)
    return json.dumps([entry])


def _auth_code_discovery(base, token_endpoint=None):
    """A Gitea-shaped discovery document: no device grant, S256
    supported. ``token_endpoint`` defaults to the Gitea path; pass the
    entry's token URL to make the document describe that server."""
    return json.dumps(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/login/oauth/authorize",
            "token_endpoint": token_endpoint
            or f"{base}/login/oauth/access_token",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["plain", "S256"],
        }
    ).encode()


def _device_discovery(base, token_endpoint=None):
    """A GitLab-shaped device-capable document: the device grant listed
    in grant_types_supported with no device_authorization_endpoint (that
    key is an OIDC extension GitLab does not emit)."""
    return json.dumps(
        {
            "issuer": base,
            "token_endpoint": token_endpoint or f"{base}/oauth/token",
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
                "device_code",
            ],
            "code_challenge_methods_supported": ["S256"],
        }
    ).encode()


def _device_providers_env(base, **overrides):
    entry = {
        "host": "git.example.com",
        "client_id": "cid-e2e",
        "device_code_url": f"{base}/oauth/authorize_device",
        "token_url": f"{base}/oauth/token",
    }
    entry.update(overrides)
    return json.dumps([entry])


class TestPkceVectors:
    """The PKCE mechanics against RFC 7636 appendix B."""

    def test_s256_challenge_matches_rfc7636_appendix_b(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        assert (
            helper.s256_challenge(verifier)
            == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )

    def test_make_pkce_pair_shape(self):
        verifier, challenge = helper.make_pkce_pair()
        assert len(verifier) == helper.PKCE_VERIFIER_LEN
        assert set(verifier) <= set(helper.UNRESERVED)
        assert challenge == helper.s256_challenge(verifier)
        assert len(challenge) == 43  # base64url(sha256), padding stripped


class TestProviderFlowSchema:
    """The provider map's flow discriminator and per-flow fields."""

    def _entry(self, **overrides):
        entry = {
            "host": "git.example.com",
            "client_id": "cid",
            "device_code_url": "https://h/oauth/authorize_device",
            "token_url": "https://h/oauth/token",
        }
        entry.update(overrides)
        return entry

    def test_flow_defaults_to_device_code(self):
        provider = helper._provider_entry(self._entry())
        assert provider is not None
        assert provider["flow"] == helper.FLOW_DEVICE_CODE

    def test_pkce_entry_requires_authorize_url_and_redirect_uri(self):
        provider = helper._provider_entry(
            self._entry(
                flow="authorization_code_pkce",
                authorize_url="https://h/login/oauth/authorize",
            )
        )
        assert provider is None  # redirect_uri missing
        provider = helper._provider_entry(
            self._entry(
                flow="authorization_code_pkce",
                authorize_url="https://h/login/oauth/authorize",
                redirect_uri="http://127.0.0.1:8124/oauth/callback",
            )
        )
        assert provider is not None
        assert provider["flow"] == helper.FLOW_AUTH_CODE_PKCE
        assert provider["redirect_uri"].endswith("/oauth/callback")

    def test_unknown_flow_value_is_skipped(self):
        assert helper._provider_entry(self._entry(flow="implicit")) is None

    def test_stock_providers_stay_device_flow(self, monkeypatch):
        monkeypatch.setenv("KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID", "gh-id")
        providers = helper._stock_providers()
        assert providers["github.com"]["flow"] == helper.FLOW_DEVICE_CODE


class TestAuthorizeUrl:
    def test_authorize_url_carries_pkce_and_state(self):
        provider = {
            "client_id": "cid",
            "authorize_url": "https://h/login/oauth/authorize",
            "redirect_uri": "http://127.0.0.1:8124/",
            "scope": "",
        }
        url = helper.authorize_url(provider, "the-challenge", "the-state")
        parsed = urllib.parse.urlsplit(url)
        assert parsed.netloc == "h"
        params = urllib.parse.parse_qs(parsed.query)
        assert params["response_type"] == ["code"]
        assert params["client_id"] == ["cid"]
        assert params["code_challenge"] == ["the-challenge"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] == ["the-state"]
        assert params["redirect_uri"] == ["http://127.0.0.1:8124/"]
        assert "scope" not in params  # omitted when the entry sets none

    def test_scope_sent_when_entry_sets_one(self):
        provider = {
            "client_id": "cid",
            "authorize_url": "https://h/a",
            "redirect_uri": "http://127.0.0.1:8124/cb",
            "scope": "read:repository",
        }
        params = urllib.parse.parse_qs(
            urllib.parse.urlsplit(
                helper.authorize_url(provider, "c", "s")
            ).query
        )
        assert params["scope"] == ["read:repository"]


class TestFlowSelectionMatrix:
    """select_flow: the discovery document describes the server; the
    entry is the activation point; challenges catch stale device entries
    for Gitea-family hosts (#3385)."""

    def _run(
        self,
        bridge_server,
        fake_browser_id,
        providers_env,
        stdin_host="git.example.com",
        extra_stdin="",
    ):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
            "get": json.dumps({"username": "u", "password": "p"}).encode(),
        }
        return run_helper(
            "get",
            f"protocol=https\nhost={stdin_host}\n{extra_stdin}\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": providers_env(base),
            },
            extra_path=str(fake_browser_id),
        )

    def test_pkce_flow_uses_pat_dialog_until_the_relay_lands(
        self, bridge_server, fake_browser_id
    ):
        """A PKCE entry whose discovery confirms the grant resolves to
        the authorization-code flow; phase 1 has no browser relay, so the
        PAT dialog is the fallback."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(base)
        }
        result = self._run(bridge_server, fake_browser_id, _pkce_providers_env)
        assert result.returncode == 0
        assert "username=u" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert ops[0] == "peek"
        assert "get" in ops  # fell through to the PAT dialog
        assert "device_flow_show" not in ops

    def test_discovery_confirms_device_flow(
        self, bridge_server, fake_browser_id
    ):
        """A device entry whose discovery advertises the device endpoint
        keeps the device flow."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _device_discovery(base),
            "/oauth/authorize_device": json.dumps(
                {
                    "device_code": "dc",
                    "user_code": "UC-1",
                    "verification_uri": f"{base}/verify",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            "/oauth/token": json.dumps(
                {
                    "access_token": "tok",
                    "token_type": "bearer",
                    "expires_in": 3600,
                }
            ).encode(),
        }
        result = self._run(
            bridge_server, fake_browser_id, _device_providers_env
        )
        assert result.returncode == 0
        assert "password=tok" in result.stdout
        # the token response rides through: expiry rides along for
        # git >= 2.46
        assert "password_expiry_utc=" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" in ops

    def test_discovery_overrides_a_stale_entry(
        self, bridge_server, fake_browser_id
    ):
        """The entry says device_code but the server (a trusted document
        — its token_endpoint names the entry's token URL) advertises
        authorization_code only: the document wins, and (phase 1) the PAT
        dialog follows."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(
                base, token_endpoint=f"{base}/oauth/token"
            ),
            "/oauth/authorize_device": json.dumps(
                {
                    "device_code": "dc",
                    "user_code": "UC-1",
                    "verification_uri": f"{base}/verify",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            "/oauth/token": json.dumps(
                {"access_token": "tok", "token_type": "bearer"}
            ).encode(),
        }
        result = self._run(
            bridge_server, fake_browser_id, _device_providers_env
        )
        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" not in ops
        assert "get" in ops

    def test_foreign_discovery_document_does_not_override(
        self, bridge_server, fake_browser_id
    ):
        """A document whose token_endpoint names a different server (a
        path-prefixed provider reading the outer origin's document) is
        foreign: the entry's flow stands."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            # auth_code-only doc for some OTHER app on this origin
            "/.well-known/openid-configuration": _auth_code_discovery(base),
            "/oauth/authorize_device": json.dumps(
                {
                    "device_code": "dc",
                    "user_code": "UC-1",
                    "verification_uri": f"{base}/verify",
                    "interval": 0,
                    "expires_in": 60,
                }
            ).encode(),
            "/oauth/token": json.dumps(
                {"access_token": "tok", "token_type": "bearer"}
            ).encode(),
        }
        result = self._run(
            bridge_server, fake_browser_id, _device_providers_env
        )
        assert result.returncode == 0
        assert "password=tok" in result.stdout  # device flow ran

    def test_pkce_entry_flipped_to_device_falls_back_not_crash(
        self, bridge_server, fake_browser_id
    ):
        """A trusted document advertising the device grant (GitLab's
        shape) flips a PKCE entry — which carries no device_code_url — to
        the device flow. The helper must fall back to the PAT dialog, not
        crash (round-2 review: KeyError device_code_url)."""
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _device_discovery(
                base, token_endpoint=f"{base}/login/oauth/access_token"
            )
        }
        result = self._run(bridge_server, fake_browser_id, _pkce_providers_env)
        assert result.returncode == 0
        assert "Traceback" not in result.stderr
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" not in ops
        assert "get" in ops

    def test_pkce_entry_with_no_document_uses_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        """No discovery answer (404): the PKCE entry's flow stands, and
        phase 1's stub answers with the PAT dialog."""
        server, port = bridge_server
        _BridgeHandler.routes = {}  # every discovery GET answers 404
        result = self._run(bridge_server, fake_browser_id, _pkce_providers_env)
        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "get" in ops
        assert "device_flow_show" not in ops

    def test_discovery_advertising_neither_grant_uses_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": json.dumps(
                {"grant_types_supported": ["client_credentials"]}
            ).encode()
        }
        result = self._run(bridge_server, fake_browser_id, _pkce_providers_env)
        assert result.returncode == 0
        assert "get" in [r["operation"] for r in _BridgeHandler.requests]

    def test_gitea_challenge_rejects_a_stale_device_entry(
        self, bridge_server, fake_browser_id
    ):
        """No discovery document (404), but the host challenges with
        Basic realm="Gitea": a device entry for it is stale -- Gitea has
        no device flow -- so the PAT dialog answers."""
        _BridgeHandler.routes = {}  # every discovery GET answers 404
        result = self._run(
            bridge_server,
            fake_browser_id,
            _device_providers_env,
            extra_stdin='wwwauth[]=Basic realm="Gitea"\nwwwauth[]=Bearer',
        )
        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "device_flow_show" not in ops
        assert "get" in ops

    def test_flow_from_discovery_unit(self):
        # device via the grant list (GitLab's real shape: no
        # device_authorization_endpoint)
        assert (
            helper.flow_from_discovery(
                {
                    "grant_types_supported": [
                        "authorization_code",
                        "device_code",
                    ]
                }
            )
            == helper.FLOW_DEVICE_CODE
        )
        # device via the endpoint key (an OIDC extension some providers
        # do emit)
        assert (
            helper.flow_from_discovery({"device_authorization_endpoint": "x"})
            == helper.FLOW_DEVICE_CODE
        )
        # device via the RFC 8628 urn spelling
        assert (
            helper.flow_from_discovery(
                {
                    "grant_types_supported": [
                        "urn:ietf:params:oauth:grant-type:device_code"
                    ]
                }
            )
            == helper.FLOW_DEVICE_CODE
        )
        # authorization_code only, with S256 (Gitea's shape)
        assert (
            helper.flow_from_discovery(
                {
                    "grant_types_supported": ["authorization_code"],
                    "code_challenge_methods_supported": ["S256"],
                }
            )
            == helper.FLOW_AUTH_CODE_PKCE
        )
        assert (
            helper.flow_from_discovery(
                {
                    "grant_types_supported": ["authorization_code"],
                    "code_challenge_methods_supported": ["plain"],
                }
            )
            is None
        )
        assert helper.flow_from_discovery("not-a-dict") is None
        assert helper.flow_from_discovery({}) is None

    def test_wwwauth_realms_parses_multiple_challenges(self):
        cred = {"wwwauth": ['Basic realm="Gitea"', 'Bearer realm="other"']}
        assert helper.wwwauth_realms(cred) == ["gitea", "other"]
        assert helper.is_gitea_family(cred)
        assert not helper.is_gitea_family({"wwwauth": ['Basic realm="x"']})
        assert not helper.is_gitea_family({})

    def test_wwwauth_realms_tolerates_quoting_variants(self):
        """RFC 7235: case-insensitive parameter names; realm values may
        be single-quoted or bare tokens."""
        cred = {
            "wwwauth": [
                "Basic Realm='Forgejo'",
                "Basic realm=gitea.example.com",
                'Basic realm=""',
            ]
        }
        assert helper.wwwauth_realms(cred) == [
            "forgejo",
            "gitea.example.com",
            "",
        ]
        assert helper.is_gitea_family(cred)


class TestDevicePollSteps:
    """The poll loop's wait/stop ladder (RFC 8628 section 3.5)."""

    def test_pending_waits_and_slow_down_backs_off(self):
        assert helper._poll_failure_step(
            {"error": "authorization_pending"}, 5
        ) == ("wait", 5)
        assert helper._poll_failure_step({"error": "slow_down"}, 5) == (
            "wait",
            10,
        )

    def test_terminal_errors_stop_with_named_texts(self):
        kind, resp = helper._poll_failure_step({"error": "expired_token"}, 5)
        assert kind == "stop"
        assert (
            helper._poll_error_text(resp) == "Code expired. Please try again."
        )
        kind, resp = helper._poll_failure_step({"error": "access_denied"}, 5)
        assert kind == "stop"
        assert helper._poll_error_text(resp) == "Authorization denied."
        kind, resp = helper._poll_failure_step(
            {"error": "server_error", "error_description": "boom"}, 5
        )
        assert kind == "stop"
        assert helper._poll_error_text(resp) == "boom"


class TestTokenRequestShapes:
    """exchange_code / refresh_access_token request bodies (public
    client: no secret anywhere)."""

    def _provider(self, base):
        return {
            "host": "git.example.com",
            "client_id": "cid-e2e",
            "flow": "authorization_code_pkce",
            "authorize_url": f"{base}/login/oauth/authorize",
            "token_url": f"{base}/login/oauth/access_token",
            "redirect_uri": "http://127.0.0.1:8124/",
            "scope": "",
            "username": "oauth2",
        }

    def test_exchange_code_posts_expected_fields(self, bridge_server):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/login/oauth/access_token": json.dumps(
                {
                    "access_token": "tok",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                }
            ).encode()
        }
        resp = helper.exchange_code(
            self._provider(base), "the-code", "the-verifier"
        )
        assert resp["access_token"] == "tok"
        form = _BridgeHandler.forms[0]
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "the-code"
        assert form["code_verifier"] == "the-verifier"
        assert form["client_id"] == "cid-e2e"
        assert form["redirect_uri"] == "http://127.0.0.1:8124/"
        assert "client_secret" not in form

    def test_refresh_posts_expected_fields(self, bridge_server):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.routes = {
            "/login/oauth/access_token": json.dumps(
                {"access_token": "tok2"}
            ).encode()
        }
        resp = helper.refresh_access_token(
            self._provider(base), "the-refresh-token"
        )
        assert resp["access_token"] == "tok2"
        form = _BridgeHandler.forms[0]
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "the-refresh-token"
        assert form["client_id"] == "cid-e2e"
        assert "client_secret" not in form

    def test_unreachable_token_endpoint_returns_none(self):
        provider = self._provider("http://127.0.0.1:1")
        assert helper.exchange_code(provider, "c", "v") is None
        assert helper.refresh_access_token(provider, "r") is None


class TestCredentialOutput:
    def test_extras_ride_along_when_the_provider_supplies_them(self):
        out = helper.credential_output(
            "oauth2", "tok", {"refresh_token": "rt", "expires_in": 3600}
        )
        lines = out.splitlines()
        assert lines[0] == "username=oauth2"
        assert lines[1] == "password=tok"
        assert "oauth_refresh_token=rt" in lines
        expiry = next(
            ln for ln in lines if ln.startswith("password_expiry_utc=")
        )
        # a 3600s token expires roughly an hour from now
        assert 3500 < int(expiry.split("=")[1]) - int(time.time()) <= 3600

    def test_plain_output_without_token_response(self):
        assert helper.credential_output("u", "p") == "username=u\npassword=p"
        assert (
            helper.credential_output("u", "p", {}) == "username=u\npassword=p"
        )


class TestAuthorizationCodeFlow:
    """The end-to-end browser flow (#3385): the helper relays the
    authorize URL, the bridge answers with the code (echoing the helper's
    state), the exchange runs container-side, and the OAuth credential
    (with its refresh token) lands in the tab cache immediately."""

    def _setup(
        self,
        bridge_server,
        code="the-code",
        state_transform=lambda s: s,
        token_body=None,
    ):
        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps({"error": "miss"}).encode(),
        }
        _BridgeHandler.op_handlers = {
            "auth_flow_start": lambda p: {
                "code": code,
                "state": state_transform(p["state"]),
            },
        }
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(
                base, token_endpoint=f"{base}/login/oauth/access_token"
            ),
            "/login/oauth/access_token": json.dumps(
                token_body
                or {
                    "access_token": "tok-live",
                    "refresh_token": "rt-live",
                    "expires_in": 3600,
                }
            ).encode(),
        }
        return base

    def _run(self, bridge_server, fake_browser_id, base):
        return run_helper(
            "get",
            "protocol=https\nhost=git.example.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": _pkce_providers_env(base),
            },
            extra_path=str(fake_browser_id),
        )

    def test_full_flow_exchanges_and_caches(
        self, bridge_server, fake_browser_id
    ):
        base = self._setup(bridge_server)
        result = self._run(bridge_server, fake_browser_id, base)
        assert result.returncode == 0
        assert "username=oauth2" in result.stdout
        assert "password=tok-live" in result.stdout
        assert "oauth_refresh_token=rt-live" in result.stdout
        assert "password_expiry_utc=" in result.stdout
        # The authorize URL carried PKCE + the registered redirect.
        start = next(
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "auth_flow_start"
        )
        assert start["host"] == "git.example.com"
        assert "code_challenge_method=S256" in start["authorize_url"]
        # The registered redirect is the klangk origin root.
        assert (
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8124%2F"
            in start["authorize_url"]
        )
        # The PKCE verifier and any secret never enter the relay.
        assert "code_verifier" not in start
        assert "client_secret" not in start
        # The exchange posted the verifier container-side.
        exchange = _BridgeHandler.forms[0]
        assert exchange["grant_type"] == "authorization_code"
        assert exchange["code"] == "the-code"
        assert "code_verifier" in exchange
        assert "client_secret" not in exchange
        # The OAuth credential (with refresh fields) was stored in the
        # tab immediately — git's own store cannot carry them.
        store = next(
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "store"
            and r.get("refresh_token") == "rt-live"
        )
        assert store["password"] == "tok-live"
        assert store["expires_at"] > 0

    def test_state_mismatch_falls_back_to_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        base = self._setup(bridge_server, state_transform=lambda s: "not-" + s)
        _BridgeHandler.op_bodies["get"] = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()
        result = self._run(bridge_server, fake_browser_id, base)
        assert result.returncode == 0
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "get" in ops  # PAT dialog answered
        # No exchange happened: the mismatched code was discarded.
        assert _BridgeHandler.forms == []

    def test_user_cancel_falls_back_to_pat_dialog(
        self, bridge_server, fake_browser_id
    ):
        base = self._setup(bridge_server)
        _BridgeHandler.op_handlers = {
            "auth_flow_start": lambda p: {"error": "cancelled"},
        }
        _BridgeHandler.op_bodies["get"] = json.dumps(
            {"username": "u", "password": "p"}
        ).encode()
        result = self._run(bridge_server, fake_browser_id, base)
        assert result.returncode == 0
        assert "username=u" in result.stdout
        assert _BridgeHandler.forms == []


class TestRefreshFirst:
    """A cached OAuth token inside the skew window refreshes headlessly
    (#3385): no browser flow, no PAT dialog."""

    def _run_with_cache(self, bridge_server, fake_browser_id, cached, base):
        _BridgeHandler.op_bodies = {
            "peek": json.dumps(cached).encode(),
        }
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(
                base, token_endpoint=f"{base}/login/oauth/access_token"
            ),
            "/login/oauth/access_token": json.dumps(
                {"access_token": "tok-fresh", "refresh_token": "rt2"}
            ).encode(),
        }
        return run_helper(
            "get",
            "protocol=https\nhost=git.example.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": _pkce_providers_env(base),
            },
            extra_path=str(fake_browser_id),
        )

    def test_expiring_token_refreshes_headlessly(
        self, bridge_server, fake_browser_id
    ):
        import time as _time

        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_with_cache(
            bridge_server,
            fake_browser_id,
            cached={
                "username": "oauth2",
                "password": "tok-stale",
                "refresh_token": "rt-1",
                "expires_at": int(_time.time()) + 30,
            },
            base=base,
        )
        assert result.returncode == 0
        assert "password=tok-fresh" in result.stdout
        # The refresh grant ran container-side...
        assert _BridgeHandler.forms[0]["grant_type"] == "refresh_token"
        assert _BridgeHandler.forms[0]["refresh_token"] == "rt-1"
        # ...and the refreshed credential was pushed back to the cache.
        store = next(
            r
            for r in _BridgeHandler.requests
            if r["operation"] == "store" and r.get("password") == "tok-fresh"
        )
        assert store["refresh_token"] == "rt2"
        # No browser flow, no PAT dialog.
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "auth_flow_start" not in ops
        assert "get" not in ops

    def test_fresh_token_is_used_as_is(self, bridge_server, fake_browser_id):
        import time as _time

        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        result = self._run_with_cache(
            bridge_server,
            fake_browser_id,
            cached={
                "username": "oauth2",
                "password": "tok-good",
                "refresh_token": "rt-1",
                "expires_at": int(_time.time()) + 3600,
            },
            base=base,
        )
        assert result.returncode == 0
        assert "password=tok-good" in result.stdout
        assert _BridgeHandler.forms == []

    def test_rejected_refresh_runs_a_fresh_flow(
        self, bridge_server, fake_browser_id
    ):
        """A rejected refresh grant must not serve the stale token: the
        dead entry is erased and a fresh browser flow starts (#3385
        review round 1)."""
        import time as _time

        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps(
                {
                    "username": "oauth2",
                    "password": "tok-stale",
                    "refresh_token": "rt-dead",
                    "expires_at": int(_time.time()) - 10,
                }
            ).encode(),
        }
        _BridgeHandler.op_handlers = {
            "auth_flow_start": lambda p: {
                "code": "fresh-code",
                "state": p["state"],
            },
        }
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(
                base, token_endpoint=f"{base}/login/oauth/access_token"
            ),
        }
        _BridgeHandler.route_handlers = {
            "/login/oauth/access_token": lambda form: (
                {"error": "invalid_grant"}
                if form.get("grant_type") == "refresh_token"
                else {"access_token": "tok-new"}
            ),
        }
        result = run_helper(
            "get",
            "protocol=https\nhost=git.example.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": _pkce_providers_env(base),
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 0
        assert "password=tok-new" in result.stdout  # fresh flow ran
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "erase" in ops  # the dead entry was dropped
        assert "auth_flow_start" in ops

    def test_unreachable_refresh_keeps_the_cached_credential(
        self, bridge_server, fake_browser_id
    ):
        """A transient refresh failure (endpoint down / garbage) keeps
        the cached credential instead of erasing it — a network blip
        must not force re-authorization (#3385 review round 2)."""
        import time as _time

        server, port = bridge_server
        base = f"http://127.0.0.1:{port}"
        _BridgeHandler.op_bodies = {
            "peek": json.dumps(
                {
                    "username": "oauth2",
                    "password": "tok-maybe-good",
                    "refresh_token": "rt-1",
                    "expires_at": int(_time.time()) + 30,
                }
            ).encode(),
        }
        _BridgeHandler.routes = {
            "/.well-known/openid-configuration": _auth_code_discovery(
                base, token_endpoint=f"{base}/login/oauth/access_token"
            ),
            # The token endpoint answers unparseable bytes: unreachable
            # in helper terms (not a rejected grant).
            "/login/oauth/access_token": b"gateway garbage",
        }
        result = run_helper(
            "get",
            "protocol=https\nhost=git.example.com\n\n",
            env_override={
                "KLANGKWS_BRIDGE_URL": base,
                "KLANGKWS_FEATURE_OAUTH_PROVIDERS": _pkce_providers_env(base),
            },
            extra_path=str(fake_browser_id),
        )
        assert result.returncode == 0
        assert "password=tok-maybe-good" in result.stdout
        ops = [r["operation"] for r in _BridgeHandler.requests]
        assert "erase" not in ops
        assert "auth_flow_start" not in ops
