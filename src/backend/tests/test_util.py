"""Tests for util: file- and command-backed secret resolution."""

from klangk_backend.settings import resolve_dynamic_config
from klangk_backend.util import (
    Util,
    read_file_value,
    run_cmd_value,
    resolve_file_value,
    sanitize_disposition_name,
)
from _helpers import make_settings
import types


def _util(env=None):
    """Build a Util instance from explicit env."""
    settings = make_settings(env)
    return Util(types.SimpleNamespace(settings=settings))


class TestReadFileValue:
    """read_file_value is the shared helper behind resolve_dynamic_config
    and resolve_file_value."""

    def test_reads_and_strips_contents(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("from-file\n")
        contents, err = read_file_value(f"file:{f}")
        assert contents == "from-file"
        assert err is None

    def test_missing_file_returns_error(self):
        contents, err = read_file_value("file:/no/such/file")
        assert contents is None
        assert isinstance(err, OSError)
        assert err.filename == "/no/such/file"


class TestRunCmdValue:
    """run_cmd_value is the cmd: counterpart of read_file_value."""

    def test_runs_and_strips_stdout(self):
        contents, err = run_cmd_value("cmd:printf 'from-cmd\\n'")
        assert contents == "from-cmd"
        assert err is None

    def test_pipe_and_shell_features(self):
        contents, err = run_cmd_value("cmd:echo hello | tr a-z A-Z")
        assert contents == "HELLO"
        assert err is None

    def test_nonzero_exit_returns_error(self):
        contents, err = run_cmd_value("cmd:false")
        assert contents is None
        assert err is not None
        assert "exited with code" in err

    def test_no_output_is_none(self):
        # A command that succeeds but prints nothing yields empty stdout,
        # which we surface as the stripped empty string (not an error).
        contents, err = run_cmd_value("cmd:true")
        assert contents == ""
        assert err is None

    def test_timeout_returns_error(self, monkeypatch):
        import klangk_backend.util as util

        monkeypatch.setattr(util, "_CMD_TIMEOUT_SECONDS", 0.1)
        contents, err = run_cmd_value("cmd:sleep 1")
        assert contents is None
        assert err is not None
        assert "timed out" in err

    def test_execution_failure_returns_error(self, monkeypatch):
        import klangk_backend.util as util

        def _boom(*a, **k):
            raise OSError("no shell")

        monkeypatch.setattr(util.subprocess, "run", _boom)
        contents, err = run_cmd_value("cmd:anything")
        assert contents is None
        assert err == "no shell"


class TestResolveDynamicConfig:
    """resolve_dynamic_config resolves plugin-declared dynamic keys (outside
    the KLANGK_ settings model) with file:/cmd: deref."""

    def test_plain_value(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "plain-value")
        assert resolve_dynamic_config("TEST_SECRET") == "plain-value"

    def test_file_prefix_reads_file(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("from-file\n")
        monkeypatch.setenv("TEST_SECRET", f"file:{secret_file}")
        assert resolve_dynamic_config("TEST_SECRET") == "from-file"

    def test_file_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "file:/no/such/file")
        assert resolve_dynamic_config("TEST_SECRET") is None

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert resolve_dynamic_config("TEST_SECRET") is None

    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert resolve_dynamic_config("TEST_SECRET", "fallback") == "fallback"

    def test_empty_string_returned_as_is(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "")
        assert resolve_dynamic_config("TEST_SECRET") == ""

    def test_cmd_prefix_runs_command(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:printf 'from-cmd'")
        assert resolve_dynamic_config("TEST_SECRET") == "from-cmd"

    def test_cmd_prefix_with_pipe(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:echo hi | tr a-z A-Z")
        assert resolve_dynamic_config("TEST_SECRET") == "HI"

    def test_cmd_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:false")
        assert resolve_dynamic_config("TEST_SECRET") is None


class TestResolveFileValue:
    def test_plain_value(self):
        assert resolve_file_value("plain") == "plain"

    def test_file_prefix(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("from-file\n")
        assert resolve_file_value(f"file:{f}") == "from-file"

    def test_file_missing_returns_empty(self):
        assert resolve_file_value("file:/no/such/file") == ""

    def test_cmd_prefix(self):
        assert resolve_file_value("cmd:printf from-cmd") == "from-cmd"

    def test_cmd_failure_returns_empty(self):
        assert resolve_file_value("cmd:false") == ""


class TestCustomizeDir:
    def test_returns_env_value(self):
        u = _util({"KLANGK_CUSTOMIZE_DIR": "/opt/custom"})
        assert u.customize_dir() == "/opt/custom"

    def test_defaults_to_state_dir_custom(self):
        u = _util({"KLANGK_STATE_DIR": "/tmp/state"})
        assert u.customize_dir() == "/tmp/state/custom"


class TestSanitizeDispositionName:
    def test_plain_name(self):
        assert sanitize_disposition_name("file.txt") == "file.txt"

    def test_strips_double_quotes(self):
        assert sanitize_disposition_name('f"name.txt') == "fname.txt"

    def test_replaces_slashes_with_underscore(self):
        assert sanitize_disposition_name("a/b\\c") == "a_b_c"

    def test_combined(self):
        assert sanitize_disposition_name('my/"file".txt') == "my_file.txt"


class TestCorsOrigins:
    """Util.cors_origins (moved from main.py, #1503)."""

    def test_default_localhost(self):
        u = _util({})
        assert u.cors_origins() == ["http://localhost"]

    def test_egress_port_not_synthesized(self):
        """KLANGK_EGRESS_PORT does not leak into the CORS origin."""
        u = _util({"KLANGK_EGRESS_PORT": "9000"})
        assert u.cors_origins() == ["http://localhost"]

    def test_hosting_hostname_carries_port(self):
        u = _util({"KLANGK_HOSTING_HOSTNAME": "localhost:8996"})
        assert u.cors_origins() == ["http://localhost:8996"]

    def test_hosting_hostname(self):
        u = _util(
            {
                "KLANGK_HOSTING_HOSTNAME": "klangk.example.com",
                "KLANGK_HOSTING_PROTO": "https",
            }
        )
        assert u.cors_origins() == ["https://klangk.example.com"]

    def test_hosting_hostname_default_proto(self):
        u = _util({"KLANGK_HOSTING_HOSTNAME": "klangk.example.com"})
        assert u.cors_origins() == ["http://klangk.example.com"]

    def test_explicit_origins(self):
        u = _util(
            {
                "KLANGK_CORS_ORIGINS": "https://a.example.com, https://b.example.com"
            }
        )
        assert u.cors_origins() == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_explicit_origins_strips_empties(self):
        u = _util({"KLANGK_CORS_ORIGINS": "https://a.com,,"})
        assert u.cors_origins() == ["https://a.com"]

    def test_explicit_overrides_hosting(self):
        u = _util(
            {
                "KLANGK_CORS_ORIGINS": "https://override.com",
                "KLANGK_HOSTING_HOSTNAME": "ignored.com",
            }
        )
        assert u.cors_origins() == ["https://override.com"]


# --- trusted_proxy_cidrs / peer_trusted (moved from test_wshandler.py, #1503) ---


class TestTrustedProxyCidrs:
    def test_load_defaults_when_unset(self):
        import ipaddress

        trusted = _util({}).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted

    def test_load_cidr_network_token(self):
        import ipaddress

        trusted = _util(
            {"KLANGK_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 192.168.1.5"}
        ).trusted_proxy_cidrs()
        assert ipaddress.ip_network("10.0.0.0/8") in trusted
        assert ipaddress.ip_address("192.168.1.5") in trusted

    def test_load_invalid_token_warns_and_skipped(self, caplog):
        import ipaddress
        import logging

        with caplog.at_level(logging.WARNING, logger="klangk_backend.util"):
            trusted = _util(
                {"KLANGK_TRUSTED_PROXY_CIDRS": "not-an-ip, 127.0.0.1"}
            ).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted
        # The invalid entry is logged without echoing its value (env-var-
        # derived data is treated as potentially sensitive by CodeQL).
        assert any(
            "invalid KLANGK_TRUSTED_PROXY_CIDRS entry" in r.getMessage()
            for r in caplog.records
        )

    def test_load_all_invalid_falls_back_to_loopback(self):
        import ipaddress

        trusted = _util(
            {"KLANGK_TRUSTED_PROXY_CIDRS": "garbage"}
        ).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted

    def test_load_empty_value_falls_back_to_loopback(self):
        import ipaddress

        trusted = _util(
            {"KLANGK_TRUSTED_PROXY_CIDRS": ""}
        ).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted

    def test_peer_trusted_rejects_non_ip_string(self):
        assert _util({}).peer_trusted("not-an-ip") is False

    def test_peer_trusted_rejects_none(self):
        assert _util({}).peer_trusted(None) is False


# --- derive_hosting_info (moved from test_wshandler.py, #1503) ---


class TestDeriveHostingInfo:
    def test_env_vars_take_precedence(self):
        u = _util(
            {
                "KLANGK_HOSTING_HOSTNAME": "env.example.com",
                "KLANGK_HOSTING_PROTO": "https",
                "KLANGK_HOSTING_BASE_PATH": "/app",
            }
        )
        h, p, b = u.derive_hosting_info(
            {"host": "header.example.com"}, "127.0.0.1"
        )
        assert h == "env.example.com"
        assert p == "https"
        assert b == "/app"

    def test_forwarded_headers_trusted_from_loopback_peer(self):
        """Forwarded headers honored when the peer is a trusted proxy (loopback by default)."""
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info(
            {
                "x-forwarded-host": "arctor.repoze.org",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/klangk",
            },
            "127.0.0.1",
        )
        assert h == "arctor.repoze.org"
        assert p == "https"
        assert b == "/klangk"

    def test_forwarded_headers_rejected_from_untrusted_peer(self):
        """Forwarded headers ignored when the peer is NOT a trusted proxy.

        An attacker reaching the backend directly (e.g. from a public IP)
        must not be able to poison X-Forwarded-Host to mint phishing links.
        """
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info(
            {
                "host": "localhost:8997",
                "x-forwarded-host": "evil.com",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/phish",
            },
            "203.0.113.7",
        )
        assert h == "localhost:8997"
        assert p == "http"
        assert b == ""

    def test_forwarded_headers_rejected_when_no_peer(self):
        """Forwarded headers ignored when client_host is unavailable (fail-closed)."""
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info(
            {
                "host": "localhost:8997",
                "x-forwarded-host": "evil.com",
                "x-forwarded-proto": "https",
            },
            None,
        )
        assert h == "localhost:8997"
        assert p == "http"
        assert b == ""

    def test_reject_proxy_headers_override(self):
        """KLANGK_REJECT_PROXY_HEADERS=1 forces trust off even for loopback peers."""
        u = _util(
            {
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_REJECT_PROXY_HEADERS": "1",
            }
        )
        h, p, b = u.derive_hosting_info(
            {
                "host": "localhost:8997",
                "x-forwarded-host": "evil.com",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/phish",
            },
            "127.0.0.1",
        )
        assert h == "localhost:8997"
        assert p == "http"
        assert b == ""

    def test_custom_trusted_cidr(self):
        """A non-loopback peer is trusted when its CIDR is configured."""
        u = _util(
            {
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
            }
        )
        h, p, b = u.derive_hosting_info(
            {
                "x-forwarded-host": "internal.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/klangk",
            },
            "10.5.5.5",
        )
        assert h == "internal.example.com"
        assert p == "https"
        assert b == "/klangk"

    def test_host_header_used_verbatim(self):
        """Direct access: the Host header (with its port) is used verbatim.

        nginx forwards the client's Host as both Host and X-Forwarded-Host,
        so the port the browser hit rides along unmodified — no port is
        synthesized from KLANGK_EGRESS_PORT (that is internal wiring, not the
        public port; wrong behind a real proxy/ingress).
        """
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info({"host": "myhost:8997"}, "127.0.0.1")
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_host_header_no_egress_port(self):
        u = _util({})
        h, p, b = u.derive_hosting_info({"host": "myhost:8997"}, "127.0.0.1")
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_egress_port_not_synthesized_into_host(self):
        """KLANGK_EGRESS_PORT is NOT used to compose the URL authority.

        With no env override and an uninformative (empty) request, the floor
        is bare localhost — even though KLANGK_EGRESS_PORT is set. The port
        must come from KLANGK_HOSTING_HOSTNAME or the Host header, never
        guessed from the internal egress port (#1240).
        """
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info({}, "127.0.0.1")
        assert h == "localhost"
        assert p == "http"
        assert b == ""

    def test_defaults_no_egress_port(self):
        u = _util({})
        h, p, b = u.derive_hosting_info({}, "127.0.0.1")
        assert h == "localhost"
        assert p == "http"
        assert b == ""

    # --- no request in hand (eager start: autostart / workspace create) ---
    # These exercise the path start_workspace takes: no connection
    # exists at boot, so derive_hosting_info is called with no headers and
    # must still return a port-correct floor (the bug was that the eager
    # path used to bypass this entirely and inject bare "localhost").

    def test_no_headers_env_hostname_wins(self):
        """Env override applies even with no request (#1240)."""
        u = _util(
            {
                "KLANGK_HOSTING_HOSTNAME": "klangk.example.com",
                "KLANGK_HOSTING_PROTO": "https",
                "KLANGK_HOSTING_BASE_PATH": "/klangk",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        h, p, b = u.derive_hosting_info(None, None)
        assert h == "klangk.example.com"
        assert p == "https"
        assert b == "/klangk"

    def test_no_headers_falls_back_to_localhost(self):
        """No env, no request -> bare localhost (no port synthesized)."""
        u = _util({"KLANGK_EGRESS_PORT": "8995"})
        h, p, b = u.derive_hosting_info(None, None)
        assert h == "localhost"
        assert p == "http"
        assert b == ""

    def test_no_headers_no_env_no_egress_port(self):
        """Absolute floor: bare localhost when nothing is configured."""
        u = _util({})
        h, p, b = u.derive_hosting_info(None, None)
        assert h == "localhost"
        assert p == "http"
        assert b == ""


# --- client_is_loopback (moved from test_wshandler.py, #1503) ---
# Powers the none-mode /auth/local self-defense (#1374). Must admit a real
# loopback browser, admit a request proxied by nginx (peer loopback, real
# client loopback in X-Real-IP), and reject a workspace container. Forwarded
# headers from an untrusted peer are ignored so they can't be spoofed.


class TestClientIsLoopback:
    def _hdr(self, **kw):
        return kw

    def test_direct_loopback_peer_admitted(self):
        u = _util({})
        # No forwarded headers, peer is loopback -> the peer IS the client.
        assert u.client_is_loopback(self._hdr(), "127.0.0.1") is True
        assert u.client_is_loopback(self._hdr(), "::1") is True

    def test_nginx_proxied_loopback_client_admitted(self):
        """nginx fronts uvicorn on loopback; the real browser is loopback.
        nginx set X-Real-IP to the real client (loopback) -> admit."""
        u = _util({})
        h = self._hdr(**{"x-real-ip": "127.0.0.1"})
        assert u.client_is_loopback(h, "127.0.0.1") is True

    def test_nginx_proxied_nonloopback_client_rejected(self):
        """The front-proxy bypass: a workspace container reaches nginx, nginx
        proxies to uvicorn on loopback, but X-Real-IP shows the real client
        is non-loopback -> reject (the nginx ACL alone would have admitted
        it because $remote_addr was nginx's loopback)."""
        u = _util({})
        h = self._hdr(**{"x-real-ip": "10.89.0.5"})
        assert u.client_is_loopback(h, "127.0.0.1") is False

    def test_x_forwarded_for_fallback(self):
        """Without X-Real-IP, the first hop of X-Forwarded-For is used."""
        u = _util({})
        h = self._hdr(**{"x-forwarded-for": "127.0.0.1, 10.0.0.1"})
        assert u.client_is_loopback(h, "127.0.0.1") is True
        h = self._hdr(**{"x-forwarded-for": "10.89.0.5, 127.0.0.1"})
        assert u.client_is_loopback(h, "127.0.0.1") is False

    def test_spoofed_header_from_untrusted_peer_ignored(self):
        """A direct (non-loopback) caller claims X-Real-IP=127.0.0.1 to try to
        sneak past. The trust gate ignores forwarded headers from untrusted
        peers, so its real non-loopback peer is what's evaluated -> reject."""
        u = _util({})
        h = self._hdr(**{"x-real-ip": "127.0.0.1"})
        assert u.client_is_loopback(h, "10.89.0.5") is False

    def test_direct_non_loopback_peer_rejected(self):
        u = _util({})
        assert u.client_is_loopback(self._hdr(), "10.89.0.5") is False

    def test_reject_proxy_header_forces_peer_only(self):
        """KLANGK_REJECT_PROXY_HEADERS=1 disables forwarded-header trust, so
        the loopback peer (nginx) is evaluated directly -> admit, and the
        spoofed non-loopback X-Real-IP is ignored."""
        u = _util({"KLANGK_REJECT_PROXY_HEADERS": "1"})
        h = self._hdr(**{"x-real-ip": "10.89.0.5"})
        assert u.client_is_loopback(h, "127.0.0.1") is True

    def test_missing_client_host_rejected(self):
        u = _util({})
        assert u.client_is_loopback(self._hdr(), None) is False

    def test_garbage_ip_rejected(self):
        u = _util({})
        assert u.client_is_loopback(self._hdr(), "not-an-ip") is False

    def test_empty_forwarded_headers_fall_back_to_peer(self):
        """Trusted peer but no forwarded header at all: the peer (nginx,
        loopback) is the candidate -> admit (a loopback browser hitting
        nginx directly with no X-Real-IP set is the benign case)."""
        u = _util({})
        assert u.client_is_loopback(self._hdr(), "127.0.0.1") is True

    # --- UDS mode (#1396): None client is the trusted reverse proxy ---

    def test_uds_mode_none_client_trusts_forwarded(self):
        """Over a UDS (set_uds_mode(True)), a None client is the same-uid
        nginx peer. Its X-Real-IP IS consulted — a loopback value admits
        (the loopback Browser behind nginx)."""
        u = _util({})
        u.set_uds_mode(True)
        h = self._hdr(**{"x-real-ip": "127.0.0.1"})
        assert u.client_is_loopback(h, None) is True

    def test_uds_mode_none_client_rejects_nonloopback(self):
        """Over a UDS, a None client's X-Real-IP shows non-loopback -> reject
        (a container behind nginx)."""
        u = _util({})
        u.set_uds_mode(True)
        h = self._hdr(**{"x-real-ip": "10.89.0.5"})
        assert u.client_is_loopback(h, None) is False

    def test_uds_mode_reset_restores_fail_closed(self):
        """After set_uds_mode(False), a None client is again rejected (fail
        closed — the TCP/TestClient default)."""
        u = _util({})
        u.set_uds_mode(True)
        u.set_uds_mode(False)
        assert u.client_is_loopback(self._hdr(), None) is False

    def test_uds_direct_connection_admitted(self):
        """Direct UDS connection (no nginx, no forwarded headers): client_host
        is None, uds_mode is True → treat as loopback (#1399)."""
        u = _util({})
        u.set_uds_mode(True)
        # No headers at all — direct CLI connection over UDS.
        assert u.client_is_loopback(self._hdr(), None) is True

    def test_connection_peer_trusted_uds_mode(self):
        """connection_peer_is_trusted: None client trusted only in UDS mode."""
        u = _util({})
        assert u.connection_peer_is_trusted(None) is False
        u.set_uds_mode(True)
        assert u.connection_peer_is_trusted(None) is True
