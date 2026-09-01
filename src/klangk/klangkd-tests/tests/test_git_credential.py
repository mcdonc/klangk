"""Tests for the git-credential-klangk helper script."""

import json
import os
import stat
import subprocess
import sys
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
            # The faked provider endpoints receive form-encoded bodies
            # (the helper posts urlencoded data there); they aren't bridge
            # operations, so record them as parsed forms instead.
            try:
                form = urllib.parse.parse_qs(body.decode())
                self.__class__.forms.append({k: v[0] for k, v in form.items()})
            except (UnicodeDecodeError, ValueError):
                pass
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
    _BridgeHandler.forms = []
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
        """bitbucket.org has no provider entry and no shorthand → PAT
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
