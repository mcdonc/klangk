"""Tests for util: file- and command-backed secret resolution."""

import os
import uuid


from klangk.settings import resolve_dynamic_config
from klangk.util import (
    MAX_PORT,
    Util,
    authority_has_port,
    free_port,
    is_portless_loopback_host,
    port_in_use,
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
    return Util(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


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
        import klangk.util as util

        monkeypatch.setattr(util, "_CMD_TIMEOUT_SECONDS", 0.1)
        contents, err = run_cmd_value("cmd:sleep 1")
        assert contents is None
        assert err is not None
        assert "timed out" in err

    def test_execution_failure_returns_error(self, monkeypatch):
        import klangk.util as util

        def _boom(*a, **k):
            raise OSError("no shell")

        monkeypatch.setattr(util.subprocess, "run", _boom)
        contents, err = run_cmd_value("cmd:anything")
        assert contents is None
        assert err == "no shell"


class TestResolveDynamicConfig:
    """resolve_dynamic_config resolves feature-declared dynamic keys (outside
    the KLANGKD_ settings model) with file:/cmd: deref."""

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


class TestResolveDynamicConfigFeaturesConfig:
    """The features_config: block of klangkd.yaml is a second value source
    for feature-declared keys (#1659). Precedence: env > features_config: >
    feature default. file:/cmd: prefixes on the YAML values are honored too
    (consistent with the env path)."""

    def test_features_config_plain_value_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": "from-yaml"}
        assert (
            resolve_dynamic_config("TEST_SECRET", features_config=fc)
            == "from-yaml"
        )

    def test_env_wins_over_features_config(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "from-env")
        fc = {"TEST_SECRET": "from-yaml"}
        assert (
            resolve_dynamic_config("TEST_SECRET", features_config=fc)
            == "from-env"
        )

    def test_features_config_wins_over_default(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": "from-yaml"}
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config=fc
            )
            == "from-yaml"
        )

    def test_default_used_when_key_in_neither_env_nor_features_config(
        self, monkeypatch
    ):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config={"OTHER": "x"}
            )
            == "fallback"
        )

    def test_none_used_when_no_default_and_no_source(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", features_config={"OTHER": "x"}
            )
            is None
        )

    def test_file_prefix_in_features_config_value(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("from-file\n")
        fc = {"TEST_SECRET": f"file:{secret_file}"}
        assert (
            resolve_dynamic_config("TEST_SECRET", features_config=fc)
            == "from-file"
        )

    def test_cmd_prefix_in_features_config_value(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": "cmd:printf from-yaml-cmd"}
        assert (
            resolve_dynamic_config("TEST_SECRET", features_config=fc)
            == "from-yaml-cmd"
        )

    def test_bad_file_ref_falls_through_to_default(self, monkeypatch):
        # A broken file:/cmd: ref in a YAML value does NOT abort boot (the
        # values aren't resolvable at construction); it logs and falls
        # through to the feature default — mirroring the env path's behavior.
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": "file:/no/such/file"}
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config=fc
            )
            == "fallback"
        )

    def test_bad_cmd_ref_falls_through_to_default(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": "cmd:false"}
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config=fc
            )
            == "fallback"
        )

    def test_features_config_none_preserves_env_only_behavior(
        self, monkeypatch
    ):
        # Direct callers (tests, legacy paths) that don't pass the block get
        # the pre-#1659 env-only resolution.
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert resolve_dynamic_config("TEST_SECRET", "fallback") == "fallback"

    def test_empty_string_features_config_value_returned_as_is(
        self, monkeypatch
    ):
        # A YAML value of "" is a deliberate empty (not "unset"), so it wins
        # over the default — matching how the env path treats "".
        monkeypatch.delenv("TEST_SECRET", raising=False)
        fc = {"TEST_SECRET": ""}
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config=fc
            )
            == ""
        )

    def test_empty_string_env_wins_over_features_config(self, monkeypatch):
        # env is consulted first and wins even when set to the empty string —
        # an operator who clears KLANGKWS_FEATURE_X="" to blank it for a run
        # gets "", not the durable features_config value. This matches the
        # pre-#1659 env-only behavior (empty env is still "set"); locked in
        # so a future "treat empty as unset" change is deliberate, not drift.
        monkeypatch.setenv("TEST_SECRET", "")
        fc = {"TEST_SECRET": "from-yaml"}
        assert (
            resolve_dynamic_config(
                "TEST_SECRET", "fallback", features_config=fc
            )
            == ""
        )


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
        u = _util({"KLANGKD_CUSTOMIZE_DIR": "/opt/custom"})
        assert u.customize_dir() == "/opt/custom"

    def test_defaults_to_xdg_config_home_custom(self, monkeypatch):
        # #1644: customize_dir is config (user-edited, durable), not state —
        # derives from $XDG_CONFIG_HOME, not state_dir.
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xcfg")
        u = _util({"KLANGKD_STATE_DIR": "/tmp/state"})
        assert u.customize_dir() == os.path.join(
            "/tmp/xcfg", "klangkd", "custom"
        )


class TestInstanceId:
    """Util.resolve_instance_id / instance_id / instance_id_path (#1553)."""

    def test_path_is_in_data_dir(self, tmp_path):
        u = _util({"KLANGKD_DATA_DIR": str(tmp_path)})
        assert u.instance_id_path() == tmp_path / "instance-id"

    def test_resolve_generates_and_persists_uuid(self, tmp_path):
        """First resolve generates a UUID-4 and writes it to the file."""
        u = _util({"KLANGKD_DATA_DIR": str(tmp_path)})
        result = u.resolve_instance_id()
        assert uuid.UUID(result).version == 4
        assert u.instance_id() == result
        # Persisted to <data_dir>/instance-id.
        assert u.instance_id_path().read_text().strip() == result

    def test_persisted_value_survives(self, tmp_path):
        """A second Util (fresh process) reads back the same ID from the file."""
        first = _util(
            {"KLANGKD_DATA_DIR": str(tmp_path)}
        ).resolve_instance_id()
        second = _util(
            {"KLANGKD_DATA_DIR": str(tmp_path)}
        ).resolve_instance_id()
        assert first == second

    def test_empty_file_is_recreated(self, tmp_path):
        """An empty/garbage instance-id file is regenerated, not fatal."""
        u = _util({"KLANGKD_DATA_DIR": str(tmp_path)})
        path = u.instance_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("   \n")  # whitespace-only -> treated as missing
        result = u.resolve_instance_id()
        assert uuid.UUID(result).version == 4
        assert path.read_text().strip() == result

    def test_instance_id_resolves_lazily(self, tmp_path):
        """instance_id() resolves on first use when resolve wasn't called."""
        u = _util({"KLANGKD_DATA_DIR": str(tmp_path)})
        # No resolve_instance_id() call — instance_id() does it lazily.
        result = u.instance_id()
        assert uuid.UUID(result).version == 4
        assert u.instance_id_path().read_text().strip() == result
        # Second call returns the cached value (same object, no re-read).
        assert u.instance_id() == result


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
        """KLANGKD_EGRESS_PORT does not leak into the CORS origin."""
        u = _util({"KLANGKD_EGRESS_PORT": "9000"})
        assert u.cors_origins() == ["http://localhost"]

    def test_browser_listener_port_in_origin(self):
        """Full mode without a pin: origin is the browser listener (#2732).

        cors_origins derives through derive_hosting_info(None, None), so
        the synthetic loopback floor carries KLANGKD_PORT — the origin
        browsers actually load the UI from. Headless stays bare (no
        browser listener to name).
        """
        u = _util({"KLANGKD_PORT": "8997"})
        assert u.cors_origins() == ["http://localhost:8997"]

    def test_hosting_hostname_pin_wins_over_browser_port(self):
        """The env pin is used verbatim even in full mode."""
        u = _util(
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_HOSTING_HOSTNAME": "klangk.example.com",
            }
        )
        assert u.cors_origins() == ["http://klangk.example.com"]

    def test_hosting_hostname_carries_port(self):
        u = _util({"KLANGKD_HOSTING_HOSTNAME": "localhost:8996"})
        assert u.cors_origins() == ["http://localhost:8996"]

    def test_hosting_hostname(self):
        u = _util(
            {
                "KLANGKD_HOSTING_HOSTNAME": "klangk.example.com",
                "KLANGKD_HOSTING_PROTO": "https",
            }
        )
        assert u.cors_origins() == ["https://klangk.example.com"]

    def test_hosting_hostname_default_proto(self):
        u = _util({"KLANGKD_HOSTING_HOSTNAME": "klangk.example.com"})
        assert u.cors_origins() == ["http://klangk.example.com"]

    def test_explicit_origins(self):
        u = _util(
            {
                "KLANGKD_CORS_ORIGINS": "https://a.example.com, https://b.example.com"
            }
        )
        assert u.cors_origins() == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_explicit_origins_strips_empties(self):
        u = _util({"KLANGKD_CORS_ORIGINS": "https://a.com,,"})
        assert u.cors_origins() == ["https://a.com"]

    def test_explicit_overrides_hosting(self):
        u = _util(
            {
                "KLANGKD_CORS_ORIGINS": "https://override.com",
                "KLANGKD_HOSTING_HOSTNAME": "ignored.com",
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
            {"KLANGKD_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 192.168.1.5"}
        ).trusted_proxy_cidrs()
        assert ipaddress.ip_network("10.0.0.0/8") in trusted
        assert ipaddress.ip_address("192.168.1.5") in trusted

    def test_load_invalid_token_warns_and_skipped(self, caplog):
        import ipaddress
        import logging

        with caplog.at_level(logging.WARNING, logger="klangk.util"):
            trusted = _util(
                {"KLANGKD_TRUSTED_PROXY_CIDRS": "not-an-ip, 127.0.0.1"}
            ).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted
        # The invalid entry is logged without echoing its value (env-var-
        # derived data is treated as potentially sensitive by CodeQL).
        assert any(
            "invalid KLANGKD_TRUSTED_PROXY_CIDRS entry" in r.getMessage()
            for r in caplog.records
        )

    def test_load_all_invalid_falls_back_to_loopback(self):
        import ipaddress

        trusted = _util(
            {"KLANGKD_TRUSTED_PROXY_CIDRS": "garbage"}
        ).trusted_proxy_cidrs()
        assert ipaddress.ip_address("127.0.0.1") in trusted

    def test_load_empty_value_falls_back_to_loopback(self):
        import ipaddress

        trusted = _util(
            {"KLANGKD_TRUSTED_PROXY_CIDRS": ""}
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
                "KLANGKD_HOSTING_HOSTNAME": "env.example.com",
                "KLANGKD_HOSTING_PROTO": "https",
                "KLANGKD_HOSTING_BASE_PATH": "/app",
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
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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
        """KLANGKD_REJECT_PROXY_HEADERS=1 forces trust off even for loopback peers."""
        u = _util(
            {
                "KLANGKD_EGRESS_PORT": "8995",
                "KLANGKD_REJECT_PROXY_HEADERS": "1",
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
                "KLANGKD_EGRESS_PORT": "8995",
                "KLANGKD_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
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

        the proxy forwards the client's Host as both Host and X-Forwarded-Host,
        so the port the browser hit rides along unmodified — no port is
        synthesized from KLANGKD_EGRESS_PORT (that is internal wiring, not the
        public port; wrong behind a real proxy/ingress).
        """
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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
        """KLANGKD_EGRESS_PORT is NOT used to compose the URL authority.

        With no env override and an uninformative (empty) request, the floor
        is bare localhost — even though KLANGKD_EGRESS_PORT is set. The port
        must come from KLANGKD_HOSTING_HOSTNAME or the Host header, never
        guessed from the internal egress port (#1240).
        """
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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
                "KLANGKD_HOSTING_HOSTNAME": "klangk.example.com",
                "KLANGKD_HOSTING_PROTO": "https",
                "KLANGKD_HOSTING_BASE_PATH": "/klangk",
                "KLANGKD_EGRESS_PORT": "8995",
            }
        )
        h, p, b = u.derive_hosting_info(None, None)
        assert h == "klangk.example.com"
        assert p == "https"
        assert b == "/klangk"

    def test_no_headers_falls_back_to_localhost(self):
        """No env, no request -> bare localhost (no port synthesized)."""
        u = _util({"KLANGKD_EGRESS_PORT": "8995"})
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

    # --- #2732: the synthetic loopback floor carries the browser port ---

    def test_no_headers_loopback_floor_gains_browser_port(self):
        """No env, no request, browser listener set -> localhost:<KLANGKD_PORT>.

        This is the value baked into KLANGKWS_HOSTING_HOSTNAME for the
        setup-time container start (the sandbox hosted-URL path): /hosted/
        is served on the browser listener, so the URL must name it.
        """
        u = _util({"KLANGKD_PORT": "8997"})
        h, p, b = u.derive_hosting_info(None, None)
        assert h == "localhost:8997"
        assert p == "http"
        assert b == ""

    def test_uds_host_localhost_gains_browser_port(self):
        """Host: localhost from a CLI-over-UDS handshake gains the port too."""
        u = _util({"KLANGKD_PORT": "8997"})
        h, p, b = u.derive_hosting_info({"host": "localhost"}, None)
        assert h == "localhost:8997"

    def test_host_with_port_unchanged(self):
        """A Host that already carries its port is never rewritten."""
        u = _util({"KLANGKD_PORT": "8997"})
        h, _, _ = u.derive_hosting_info({"host": "localhost:8997"}, None)
        assert h == "localhost:8997"

    def test_loopback_host_forms_gains_browser_port(self):
        """Case, 127.0.0.0/8, and bracketed ::1 do not dodge the retarget.

        Host is case-insensitive per RFC 7230 and every 127.x is a local
        synthetic value — all of them name port 80 without the append.
        ``[::1]`` is the only IPv6 form whose append is parseable.
        """
        u = _util({"KLANGKD_PORT": "8997"})
        for host in ("LOCALHOST", "LocalHost", "127.0.0.2", "[::1]"):
            h, _, _ = u.derive_hosting_info({"host": host}, None)
            assert h == f"{host}:8997", host

    def test_bare_ipv6_host_left_alone(self):
        """Bare (unbracketed) IPv6 Hosts are never appended (#2732 review).

        ``::1`` parses as port-bearing; ``::ffff:127.0.0.1`` (v4-mapped
        loopback) parses as a port-less loopback — either way a bare
        ``:port`` append would emit an unparseable authority, so both
        pass through untouched.
        """
        u = _util({"KLANGKD_PORT": "8997"})
        for host in ("::1", "::ffff:127.0.0.1"):
            h, _, _ = u.derive_hosting_info({"host": host}, None)
            assert h == host, host

    def test_untrusted_portless_host_unchanged(self):
        """A non-loopback port-less Host carries remote intent; untouched.

        Same anti-phishing posture as the forwarded-header gate: an
        untrusted peer's Host is already suspect, and rewriting it with a
        local port would only launder it.
        """
        u = _util({"KLANGKD_PORT": "8997"})
        h, _, _ = u.derive_hosting_info(
            {"host": "evil.com", "x-forwarded-host": "evil.com"},
            "203.0.113.7",
        )
        assert h == "evil.com"

    def test_forwarded_host_not_port_appended(self):
        """A trusted X-Forwarded-Host passes through verbatim (#2732).

        An outer proxy on a standard port is a deliberate deployment —
        appending the local browser port would break it.
        """
        u = _util({"KLANGKD_PORT": "8997"})
        h, _, _ = u.derive_hosting_info(
            {"x-forwarded-host": "example.com"}, "127.0.0.1"
        )
        assert h == "example.com"

    def test_env_pin_not_port_appended(self):
        """The KLANGKD_HOSTING_HOSTNAME pin wins verbatim, port or not."""
        u = _util(
            {
                "KLANGKD_PORT": "8997",
                "KLANGKD_HOSTING_HOSTNAME": "klangk.example.com",
            }
        )
        h, _, _ = u.derive_hosting_info({"host": "localhost"}, None)
        assert h == "klangk.example.com"

    def test_headless_floor_stays_bare(self):
        """Headless (no KLANGKD_PORT): nothing to point at, floor bare."""
        u = _util({})
        h, _, _ = u.derive_hosting_info(None, None)
        assert h == "localhost"


class TestAuthorityHasPort:
    """host[:port] authority parsing for browser_listener_hostname (#2732)."""

    def test_portless_names(self):
        assert not authority_has_port("localhost")
        assert not authority_has_port("example.com")
        assert not authority_has_port("[::1]")

    def test_ported_names(self):
        assert authority_has_port("localhost:8997")
        assert authority_has_port("example.com:80")
        assert authority_has_port("[::1]:8997")

    def test_non_numeric_suffix_is_not_a_port(self):
        assert not authority_has_port("host:notaport")

    def test_bare_ipv6_reports_ported(self):
        """A bare unbracketed IPv6 literal is indistinguishable from host:port.

        Documented-intentional: callers leave such an authority alone
        (no supported client sends it), which is the safe outcome.
        """
        assert authority_has_port("::1")


class TestIsPortlessLoopbackHost:
    """The synthetic-local gate for browser_listener_hostname (#2732)."""

    def test_loopback_names(self):
        assert is_portless_loopback_host("localhost")
        assert is_portless_loopback_host("LOCALHOST")
        assert is_portless_loopback_host("LocalHost")
        assert is_portless_loopback_host("127.0.0.1")
        assert is_portless_loopback_host("127.0.0.2")
        assert is_portless_loopback_host("[::1]")

    def test_ported_loopback_is_not_portless(self):
        assert not is_portless_loopback_host("localhost:8997")
        assert not is_portless_loopback_host("[::1]:8997")

    def test_remote_and_garbage_are_false(self):
        assert not is_portless_loopback_host("example.com")
        assert not is_portless_loopback_host("evil.com")
        assert not is_portless_loopback_host("")
        assert not is_portless_loopback_host("not-an-ip:xyz")

    def test_bare_ipv6_forms_left_alone(self):
        """No bare (unbracketed) IPv6 is ever retargeted (#2732 review).

        A v4-mapped loopback like ``::ffff:127.0.0.1`` slips past the
        port check (its last colon-suffix is not digits) yet parses as
        loopback — appending ``:port`` would emit an authority no URL
        parser accepts. Every colon-bearing unbracketed form is False.
        """
        assert not is_portless_loopback_host("::1")
        assert not is_portless_loopback_host("::ffff:127.0.0.1")
        # The safe append exists only for the bracketed form.
        assert is_portless_loopback_host("[::1]")


# --- effective_client_ip (#2586: workstation identity for the ---
# --- session registry) ---
# Same proxy-trust gate as client_is_loopback: forwarded headers count
# only behind a trusted peer, so a workstation identity cannot be spoofed
# by a direct caller.


class TestEffectiveClientIp:
    def _hdr(self, **kw):
        return kw

    def test_direct_peer_is_the_client(self):
        u = _util({})
        assert u.effective_client_ip(self._hdr(), "10.89.0.5") == "10.89.0.5"

    def test_real_ip_behind_trusted_proxy(self):
        u = _util({})
        h = self._hdr(**{"x-real-ip": "203.0.113.7"})
        assert u.effective_client_ip(h, "127.0.0.1") == "203.0.113.7"

    def test_forwarded_for_first_hop(self):
        u = _util({})
        h = self._hdr(**{"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
        assert u.effective_client_ip(h, "127.0.0.1") == "203.0.113.7"

    def test_spoofed_header_from_untrusted_peer_ignored(self):
        u = _util({})
        h = self._hdr(**{"x-real-ip": "203.0.113.7"})
        assert u.effective_client_ip(h, "10.89.0.5") == "10.89.0.5"

    def test_garbage_forwarded_value_falls_back_to_peer(self):
        """A trusted peer forwarding a non-IP value (garbage, or an
        append-style XFF chain where the leftmost hop is client-
        controlled text) must not become a workstation identity: the
        resolver falls back to the peer (#2586 review)."""
        u = _util({})
        for garbage in ("not-an-ip", "1.2.3.4, 5.6.7.8, evil"):
            h = self._hdr(**{"x-real-ip": garbage})
            assert u.effective_client_ip(h, "127.0.0.1") == "127.0.0.1"
        h = self._hdr(**{"x-forwarded-for": "definitely-not-an-ip"})
        assert u.effective_client_ip(h, "127.0.0.1") == "127.0.0.1"

    def test_result_is_canonicalized(self):
        """The returned address is str()-normalized, so the same IPv6
        host written two ways compares as one workstation."""
        u = _util({})
        h = self._hdr(**{"x-real-ip": "0:0:0:0:0:0:0:1"})
        assert u.effective_client_ip(h, "127.0.0.1") == "::1"

    def test_reject_proxy_headers_forces_peer_only(self):
        u = _util({"KLANGKD_REJECT_PROXY_HEADERS": "1"})
        h = self._hdr(**{"x-real-ip": "203.0.113.7"})
        assert u.effective_client_ip(h, "127.0.0.1") == "127.0.0.1"

    def test_no_client_is_none(self):
        u = _util({})
        assert u.effective_client_ip(self._hdr(), None) is None

    def test_uds_mode_none_peer_still_resolves_via_headers(self):
        """Over a UDS the None peer is the trusted proxy hop, so its
        X-Real-IP resolves the real workstation."""
        u = _util({})
        u.set_uds_mode(True)
        h = self._hdr(**{"x-real-ip": "203.0.113.7"})
        assert u.effective_client_ip(h, None) == "203.0.113.7"


# --- client_is_loopback (moved from test_wshandler.py, #1503) ---
# Powers the none-mode /auth/local self-defense (#1374). Must admit a real
# loopback browser, admit a request proxied by the proxy (peer loopback, real
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

    def test_proxy_proxied_loopback_client_admitted(self):
        """The proxy fronts uvicorn on loopback; the real browser is loopback.
        The proxy set X-Real-IP to the real client (loopback) -> admit."""
        u = _util({})
        h = self._hdr(**{"x-real-ip": "127.0.0.1"})
        assert u.client_is_loopback(h, "127.0.0.1") is True

    def test_proxy_proxied_nonloopback_client_rejected(self):
        """The front-proxy bypass: a workspace container reaches the proxy, the proxy
        proxies to uvicorn on loopback, but X-Real-IP shows the real client
        is non-loopback -> reject (the proxy ACL alone would have admitted
        it because $remote_addr was the proxy's loopback)."""
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
        """KLANGKD_REJECT_PROXY_HEADERS=1 disables forwarded-header trust, so
        the loopback peer (the proxy) is evaluated directly -> admit, and the
        spoofed non-loopback X-Real-IP is ignored."""
        u = _util({"KLANGKD_REJECT_PROXY_HEADERS": "1"})
        h = self._hdr(**{"x-real-ip": "10.89.0.5"})
        assert u.client_is_loopback(h, "127.0.0.1") is True

    def test_missing_client_host_rejected(self):
        u = _util({})
        assert u.client_is_loopback(self._hdr(), None) is False

    def test_garbage_ip_rejected(self):
        u = _util({})
        assert u.client_is_loopback(self._hdr(), "not-an-ip") is False

    def test_empty_forwarded_headers_fall_back_to_peer(self):
        """Trusted peer but no forwarded header at all: the peer (the proxy,
        loopback) is the candidate -> admit (a loopback browser hitting
        the proxy directly with no X-Real-IP set is the benign case)."""
        u = _util({})
        assert u.client_is_loopback(self._hdr(), "127.0.0.1") is True

    # --- UDS mode (#1396): None client is the trusted reverse proxy ---

    def test_uds_mode_none_client_trusts_forwarded(self):
        """Over a UDS (set_uds_mode(True)), a None client is the same-uid
        proxy peer. Its X-Real-IP IS consulted — a loopback value admits
        (the loopback Browser behind the proxy)."""
        u = _util({})
        u.set_uds_mode(True)
        h = self._hdr(**{"x-real-ip": "127.0.0.1"})
        assert u.client_is_loopback(h, None) is True

    def test_uds_mode_none_client_rejects_nonloopback(self):
        """Over a UDS, a None client's X-Real-IP shows non-loopback -> reject
        (a container behind the proxy)."""
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
        """Direct UDS connection (no proxy, no forwarded headers): client_host
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


# --- OS-level port discovery (moved from test_model.py, #1547) ---
# port_in_use / free_port are pure socket probes that live in util now;
# they have no DB dependency, so their direct tests belong here.


class TestPortDiscovery:
    def test_port_in_use_false_for_free_port(self):
        assert port_in_use(59123) is False

    def test_port_in_use_true_for_bound_port(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 59124))
            assert port_in_use(59124) is True

    def test_free_port_returns_bindable_ephemeral_port(self):
        """free_port hands back a port nothing else holds (#1393)."""
        p = free_port()
        assert isinstance(p, int)
        assert 0 < p <= MAX_PORT
        # The port must actually be bindable right now (the E2E harnesses
        # rely on this to seed KLANGKD_PORT / KLANGKD_PORT_RANGE_START).
        assert port_in_use(p) is False

    def test_free_port_is_distinct_across_calls(self):
        """Two calls don't hand back the same port while held (#1393)."""
        import socket

        a = free_port()
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", a))
        try:
            b = free_port()
            assert b != a, "free_port reused a port that is currently bound"
        finally:
            held.close()


class TestBridgeIdleTimeout:
    """Per-workspace bridge idle timeout resolution (#864)."""

    def test_default_is_30s(self):
        u = _util()
        assert u.bridge_idle_timeout_for(None) == 30.0

    def test_deploy_default_from_env(self):
        u = _util({"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "60"})
        assert u.bridge_idle_timeout_for(None) == 60.0

    def test_garbage_deploy_value_falls_back_to_30s(self):
        u = _util({"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "soon"})
        assert u.bridge_idle_timeout_for(None) == 30.0

    def test_workspace_override_wins(self):
        u = _util({"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "60"})
        ws = {"settings": {"bridge_timeout": 120}}
        assert u.bridge_idle_timeout_for(ws) == 120.0

    def test_workspace_override_with_no_deploy_default(self):
        u = _util()
        ws = {"settings": {"bridge_timeout": 90}}
        assert u.bridge_idle_timeout_for(ws) == 90.0

    def test_no_override_falls_back_to_deploy_default(self):
        u = _util({"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "45"})
        assert u.bridge_idle_timeout_for({"settings": None}) == 45.0
        assert u.bridge_idle_timeout_for({}) == 45.0
        assert u.bridge_idle_timeout_for(None) == 45.0

    def test_no_override_no_deploy_default_is_30s(self):
        u = _util()
        assert u.bridge_idle_timeout_for({"settings": None}) == 30.0
        assert u.bridge_idle_timeout_for(None) == 30.0

    def test_other_settings_key_does_not_affect_bridge(self):
        # An override for a different key must not leak into bridge resolution.
        u = _util({"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "60"})
        ws = {"settings": {"idle_timeout": 300}}
        assert u.bridge_idle_timeout_for(ws) == 60.0


class TestPeerTrustedBranchGaps2834:
    def test_unlisted_ip_iterates_every_network(self):
        # The CIDR loop-continue arm: an IP matching NO network walks
        # them all (no short-circuit) and returns False.
        u = _util({"KLANGKD_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 172.16.0.0/12"})
        assert u.peer_trusted("203.0.113.9") is False
