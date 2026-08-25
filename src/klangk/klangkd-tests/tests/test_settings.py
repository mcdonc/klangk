"""Tests for the KlangkSettings config loader (#1394).

Covers:
- file: / cmd: indirection resolution (success + error paths)
- make_settings(...) constructor + config_file= param
- resolve_dynamic_config (feature-declared dynamic keys)
"""

import os

import pytest

from _helpers import make_settings
from klangk.settings import (
    KlangkSettings,
    _resolve_indirection,
    resolve_dynamic_config,
)


class TestResolveIndirection:
    """The private ``_resolve_indirection`` is the core ``file:``/``cmd:``
    resolver — shared by the ``_resolve_indirections`` model validator on
    ``KlangkSettings`` (construction-time, #1461) and
    ``resolve_dynamic_config`` (feature-declared dynamic keys)."""

    def test_none_returns_none(self):
        assert _resolve_indirection(None) is None

    def test_plain_value(self):
        assert _resolve_indirection("hello") == "hello"

    def test_file_prefix(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("the-secret\n")
        assert _resolve_indirection(f"file:{secret}") == "the-secret"

    def test_file_failure_returns_none(self):
        result = _resolve_indirection("file:/nonexistent/path/to/secret")
        assert result is None

    def test_cmd_prefix(self):
        result = _resolve_indirection("cmd:echo hello")
        assert result == "hello"

    def test_cmd_failure_returns_none(self):
        result = _resolve_indirection("cmd:false")
        assert result is None

    def test_cmd_nonzero_exit_returns_none(self):
        result = _resolve_indirection("cmd:exit 1")
        assert result is None

    def test_cmd_oserror(self):
        # A command that can't be spawned (no such binary)
        result = _resolve_indirection("cmd:/nonexistent/binary/path")
        assert result is None

    def test_cmd_timeout(self, monkeypatch):
        # Patch the timeout short so we don't actually wait the default
        # _CMD_TIMEOUT_SECONDS (10s); the test only asserts the timeout
        # path fires. ``sleep 5`` outlasts the patched 0.5s timeout (#1989).
        monkeypatch.setattr("klangk.settings._CMD_TIMEOUT_SECONDS", 0.5)
        result = _resolve_indirection("cmd:sleep 5")
        assert result is None


class TestResolveDynamicConfig:
    """``resolve_dynamic_config`` resolves feature-declared dynamic keys
    (outside the ``KLANGKD_`` settings model) with ``file:``/``cmd:``
    deref (#1518)."""

    def test_plain_value(self, monkeypatch):
        monkeypatch.setenv("MY_FEATURE_TOKEN", "abc123")
        assert resolve_dynamic_config("MY_FEATURE_TOKEN") == "abc123"

    def test_default_when_unset(self):
        assert resolve_dynamic_config("UNSET_FEATURE_VAR", "fallback") == (
            "fallback"
        )

    def test_unset_no_default(self):
        assert resolve_dynamic_config("UNSET_FEATURE_VAR") is None

    def test_file_resolution(self, monkeypatch, tmp_path):
        secret = tmp_path / "token"
        secret.write_text("file-secret\n")
        monkeypatch.setenv("MY_FEATURE_TOKEN", f"file:{secret}")
        assert resolve_dynamic_config("MY_FEATURE_TOKEN") == "file-secret"

    def test_cmd_resolution(self, monkeypatch):
        monkeypatch.setenv("MY_FEATURE_TOKEN", "cmd:echo cmd-secret")
        assert resolve_dynamic_config("MY_FEATURE_TOKEN") == "cmd-secret"


class TestSettingsModel:
    def test_extra_ignored(self, monkeypatch):
        """Unknown KLANGKD_ keys are tolerated (extra='ignore')."""
        s = make_settings({"KLANGKD_BOGUS_KEY": "whatever"})
        assert not hasattr(s, "bogus_key")

    def test_all_klangk_fields_present(self):
        """Spot-check a few fields exist on the model."""
        fields = KlangkSettings.model_fields
        for name in (
            "jwt_secret",
            "auth_modes",
            "data_dir",
            "egress_port",
            "egress_listen",
            "port",
            "socket",
            "proxy_port",
            "llm_api_key",
            "trusted_proxy_cidrs",
            "container_subnets",
        ):
            assert name in fields, f"missing field: {name}"


# ---------------------------------------------------------------------------
# YAML config-file loading (#1395)
# ---------------------------------------------------------------------------


class TestConfigFile:
    def test_yaml_provides_values(self, tmp_path):
        """A YAML config file provides values that env doesn't override."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('logo_url: "https://example.com/logo.png"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.logo_url == "https://example.com/logo.png"

    def test_env_overrides_yaml(self, tmp_path):
        """Env vars override YAML file values (precedence)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('brand_color: "#FF0000"\n')
        s = make_settings(
            env={"KLANGKD_BRAND_COLOR": "#00FF00"}, config_file=str(cfg)
        )
        assert s.brand_color == "#00FF00"

    def test_yaml_doesnt_override_env(self, tmp_path):
        """A key set in both env and YAML: env wins."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('product_name: "From YAML"\n')
        s = make_settings(
            env={"KLANGKD_PRODUCT_NAME": "From Env"}, config_file=str(cfg)
        )
        assert s.product_name == "From Env"

    def test_config_none_opt_out(self):
        """config_file='none': no file, env+defaults only."""
        s = make_settings({}, config_file="none")
        assert s.egress_port == "8995"  # built-in default

    def test_netfilter_default_domains_from_env_comma_string(self):
        """Env delivers a comma-separated string; coerced to a validated,
        de-duped list (#1365)."""
        s = make_settings(
            {"KLANGKD_NETFILTER_DEFAULT_DOMAINS": "b.io, a.com:443 ,b.io"}
        )
        assert s.netfilter_default_domains == ["b.io", "a.com:443"]

    def test_netfilter_default_domains_from_yaml_list(self, tmp_path):
        """YAML delivers a native list (#1365)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "netfilter_default_domains:\n  - github.com:443\n  - pypi.org\n"
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.netfilter_default_domains == ["github.com:443", "pypi.org"]

    def test_netfilter_default_domains_accepts_cidr(self):
        """#1935: the deploy default accepts IPv4 CIDR specs (with and
        without a port scope), same grammar as a workspace allow-list."""
        s = make_settings(
            {
                "KLANGKD_NETFILTER_DEFAULT_DOMAINS": (
                    "10.0.0.0/8, 192.168.0.0/16:443, github.com:443"
                )
            }
        )
        assert s.netfilter_default_domains == [
            "10.0.0.0/8",
            "192.168.0.0/16:443",
            "github.com:443",
        ]

    def test_netfilter_default_domains_empty_is_none(self):
        """Empty / unset → None (no deploy default; workspaces unrestricted)."""
        assert make_settings({}).netfilter_default_domains is None
        assert (
            make_settings(
                {"KLANGKD_NETFILTER_DEFAULT_DOMAINS": "  , "}
            ).netfilter_default_domains
            is None
        )

    def test_netfilter_default_domains_invalid_spec_rejected(self):
        """#1939: a bad spec aborts construction (reverses the #1772
        warn-and-fallback). A misconfigured deploy-wide egress allow-list
        must fail loudly rather than silently leaving workspaces
        unrestricted."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings(
                {"KLANGKD_NETFILTER_DEFAULT_DOMAINS": "good.com,bad spec"}
            )
        msg = str(exc_info.value)
        assert "invalid spec" in msg
        assert "KLANGKD_NETFILTER_DEFAULT_DOMAINS" in msg

    def test_netfilter_default_domains_wrong_type_rejected(self, tmp_path):
        """#1939: a non-list/non-string value (e.g. a bare int from a
        malformed YAML block) aborts construction instead of falling back
        to None."""
        from pydantic import ValidationError

        # YAML delivering a scalar int directly (a typo'd block like
        # `netfilter_default_domains: 42` instead of a list).
        cfg = tmp_path / "config.yaml"
        cfg.write_text("netfilter_default_domains: 42\n")
        with pytest.raises(ValidationError) as exc_info:
            make_settings({}, config_file=str(cfg))
        msg = str(exc_info.value)
        assert "must be a list or a comma-separated string" in msg
        assert "KLANGKD_NETFILTER_DEFAULT_DOMAINS" in msg

    def test_netfilter_default_domains_malformed_aborts_reload(self):
        """#1939: a malformed value introduced after startup (operator edits
        KLANGKD_NETFILTER_DEFAULT_DOMAINS, then SIGHUPs) makes reload()
        raise. ``_reload_settings`` (main.py) catches that (``except
        Exception``) and denies the restart, keeping the runtime on the old
        config — so a typo in a reload no longer silently drops the default."""
        from pydantic import ValidationError

        s = make_settings({})
        # Simulate an operator edit + SIGHUP that introduces a bad value
        # into the env mapping reload() re-reads.
        s._reload_env["KLANGKD_NETFILTER_DEFAULT_DOMAINS"] = (
            "good.com,bad spec"
        )
        with pytest.raises(ValidationError) as exc_info:
            s.reload()
        assert "invalid spec" in str(exc_info.value)

    def test_netfilter_enabled_defaults_true(self):
        # #1774: netfilter is armed out of the box.
        assert make_settings({}).netfilter_enabled is True

    def test_netfilter_enabled_env_override(self):
        s = make_settings({"KLANGKD_NETFILTER_ENABLED": "false"})
        assert s.netfilter_enabled is False

    def test_enable_ping_defaults_true(self):
        # #2045: unprivileged ping is enabled out of the box.
        assert make_settings({}).enable_ping is True

    def test_enable_ping_env_override(self):
        s = make_settings({"KLANGKD_ENABLE_PING": "false"})
        assert s.enable_ping is False

    def test_per_handle_home_defaults_true(self):
        # #2719: per-handle homes stay the default until the #2169 flip.
        assert make_settings({}).per_handle_home is True

    def test_per_handle_home_env_override(self):
        s = make_settings({"KLANGKD_PER_HANDLE_HOME": "false"})
        assert s.per_handle_home is False

    # --- Container resource limits (#34) ---

    def test_container_limits_default_to_protective_caps(self):
        # #2030: a fresh config ships bounded — 2 CPUs / 8g / 16384 PIDs —
        # so a runaway workspace can't take down the host out of the box.
        # Set a field to an empty env value to disable just that one cap.
        s = make_settings({})
        assert s.container_cpu_limit == 2.0
        assert s.container_memory_limit == "8g"
        assert s.container_pids_limit == 16384

    def test_container_cpu_limit_from_env(self):
        s = make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": "1.5"})
        assert s.container_cpu_limit == 1.5

    def test_container_cpu_limit_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("container_cpu_limit: 0.75\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.container_cpu_limit == 0.75

    def test_container_cpu_limit_empty_string_is_none(self):
        s = make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": ""})
        assert s.container_cpu_limit is None

    def test_container_cpu_limit_non_numeric_aborts(self):
        # #34: a malformed value raises (construction fails → boot aborts).
        with pytest.raises(Exception, match="positive number"):
            make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": "lots"})

    def test_container_cpu_limit_zero_aborts(self):
        with pytest.raises(Exception, match="finite, positive"):
            make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": "0"})

    def test_container_cpu_limit_negative_aborts(self):
        with pytest.raises(Exception, match="finite, positive"):
            make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": "-1"})

    def test_container_cpu_limit_nan_aborts(self):
        # float("nan") parses fine but nan <= 0 is False, so without an
        # explicit isfinite check it slips through to podman create
        # (#1941 review).
        for raw in ("nan", "NaN", "NAN"):
            with pytest.raises(Exception, match="finite, positive"):
                make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": raw})

    def test_container_cpu_limit_inf_aborts(self):
        # float("inf") parses fine and inf > 0, so the <= 0 guard alone
        # wouldn't catch it; isfinite does (#1941 review).
        for raw in ("inf", "Infinity", "-inf"):
            with pytest.raises(Exception, match="finite, positive"):
                make_settings({"KLANGKD_CONTAINER_CPU_LIMIT": raw})

    def test_container_memory_limit_from_env(self):
        s = make_settings({"KLANGKD_CONTAINER_MEMORY_LIMIT": "2g"})
        assert s.container_memory_limit == "2g"

    def test_container_memory_limit_accepts_go_units_grammar(self):
        # Matches docker/go-units ParseSize (podman --memory) exactly: a
        # positive number + optional base unit (b/k/m/g/t/p, case-
        # insensitive) + optional trailing b. Decimals ok (ParseFloat).
        for raw, expected in [
            ("512m", "512m"),
            ("1024", "1024"),  # bare bytes
            ("1024b", "1024b"),  # explicit bytes
            ("1.5g", "1.5g"),  # decimal
            ("2gb", "2gb"),  # two-letter suffix
            ("512mb", "512mb"),
            ("2gB", "2gB"),  # case-insensitive suffix
            (" 8G ", "8G"),  # stripped, upper-case unit
            ("2GB", "2GB"),
            ("2t", "2t"),  # tera
            ("2tb", "2tb"),
            ("2p", "2p"),  # peta
        ]:
            assert (
                make_settings(
                    {"KLANGKD_CONTAINER_MEMORY_LIMIT": raw}
                ).container_memory_limit
                == expected
            ), raw

    def test_container_memory_limit_rejects_iec_iform(self):
        # go-units does NOT accept kib/mib/gib/... (only the single-letter
        # base + optional b), so neither do we — keeps the guard honest
        # about what podman will actually honour (#1941 review).
        for raw in ("2gib", "2Gib", "2kib", "2mib"):
            with pytest.raises(Exception, match="Expected a positive size"):
                make_settings({"KLANGKD_CONTAINER_MEMORY_LIMIT": raw})

    def test_container_memory_limit_from_yaml(self, tmp_path):
        # YAML delivers native types — an int (1024) and a str ("2g") —
        # both must round-trip through the str(v).strip() coercion in the
        # validator (#1941 review). Uses a fresh cfg per value.
        for value, expected in [(1024, "1024"), ("2g", "2g")]:
            cfg = tmp_path / f"cfg-{value}.yaml"
            cfg.write_text(f"container_memory_limit: {value}\n")
            s = make_settings({}, config_file=str(cfg))
            assert s.container_memory_limit == expected, value

    def test_container_memory_limit_empty_string_is_none(self):
        s = make_settings({"KLANGKD_CONTAINER_MEMORY_LIMIT": ""})
        assert s.container_memory_limit is None

    def test_container_memory_limit_malformed_aborts(self):
        with pytest.raises(Exception, match="Expected a positive size"):
            make_settings({"KLANGKD_CONTAINER_MEMORY_LIMIT": "2gigabytes"})

    def test_container_memory_limit_zero_aborts(self):
        # podman treats --memory=0 as "no limit", the same ambiguity the
        # PIDs validator rejects — keep zero-handling consistent (#1941
        # review). Covers 0, 0b, 0g.
        for raw in ("0", "0b", "0g"):
            with pytest.raises(Exception, match="must be > 0"):
                make_settings({"KLANGKD_CONTAINER_MEMORY_LIMIT": raw})

    def test_container_tmp_size_default_is_2g(self):
        # #2378: out-of-the-box default preserves the pre-#2378 hardcoded
        # /tmp tmpfs size (no behavior change for existing installs).
        assert make_settings({}).container_tmp_size == "2g"

    def test_container_tmp_size_from_env(self):
        s = make_settings({"KLANGKD_CONTAINER_TMP_SIZE": "512m"})
        assert s.container_tmp_size == "512m"

    def test_container_tmp_size_accepts_go_units_grammar(self):
        # Same grammar as container_memory_limit (shared regex).
        for raw, expected in [
            ("2g", "2g"),
            ("512mb", "512mb"),
            ("1024", "1024"),
        ]:
            assert (
                make_settings(
                    {"KLANGKD_CONTAINER_TMP_SIZE": raw}
                ).container_tmp_size
                == expected
            ), raw

    def test_container_tmp_size_rejects_iec_iform(self):
        with pytest.raises(Exception, match="Expected a positive size"):
            make_settings({"KLANGKD_CONTAINER_TMP_SIZE": "2gib"})

    def test_container_tmp_size_empty_string_is_none(self):
        # Empty -> None -> /tmp mounted with no size= option (podman sizes
        # it at half of RAM).
        s = make_settings({"KLANGKD_CONTAINER_TMP_SIZE": ""})
        assert s.container_tmp_size is None

    def test_container_tmp_size_zero_aborts(self):
        with pytest.raises(Exception, match="must be > 0"):
            make_settings({"KLANGKD_CONTAINER_TMP_SIZE": "0"})

    def test_container_pids_limit_from_env(self):
        s = make_settings({"KLANGKD_CONTAINER_PIDS_LIMIT": "512"})
        assert s.container_pids_limit == 512

    def test_container_pids_limit_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("container_pids_limit: 1024\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.container_pids_limit == 1024

    def test_container_pids_limit_empty_string_is_none(self):
        s = make_settings({"KLANGKD_CONTAINER_PIDS_LIMIT": ""})
        assert s.container_pids_limit is None

    def test_container_pids_limit_non_integer_aborts(self):
        with pytest.raises(Exception, match="positive integer"):
            make_settings({"KLANGKD_CONTAINER_PIDS_LIMIT": "many"})

    def test_container_pids_limit_float_from_yaml_aborts(self, tmp_path):
        # int(1.5) silently truncates to 1; a YAML float must be rejected
        # explicitly rather than silently truncated (#1941 review). The
        # env path is already covered — int("1.5") raises on its own.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("container_pids_limit: 1.5\n")
        with pytest.raises(Exception, match="got a float"):
            make_settings({}, config_file=str(cfg))

    def test_container_pids_limit_zero_aborts(self):
        # 0 means unlimited in podman, but a safety cap of "unlimited" is
        # just an unset var; reject to keep the semantics unambiguous (#34).
        with pytest.raises(Exception, match="must be > 0"):
            make_settings({"KLANGKD_CONTAINER_PIDS_LIMIT": "0"})

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values in YAML resolve at construction (#1461)."""
        secret = tmp_path / "jwt.txt"
        secret.write_text("yaml-secret\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f'jwt_secret: "file:{secret}"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.jwt_secret == "yaml-secret"

    def test_features_config_block_loaded_from_yaml(self, tmp_path):
        """The features_config: block populates the field verbatim (#1659).

        Values are kept raw (file:/cmd: prefixes intact) — they are resolved
        per-key by resolve_dynamic_config at use time, not at construction
        (the _resolve_indirections validator only processes top-level str
        fields, so a dict is left untouched). This mirrors how env values
        reach resolve_dynamic_config.
        """
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "features_config:\n"
            '  KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID: "abc123"\n'
            '  KLANGKWS_FEATURE_SOLIPLEX_URL: "https://rag.example.com"\n'
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.features_config == {
            "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc123",
            "KLANGKWS_FEATURE_SOLIPLEX_URL": "https://rag.example.com",
        }

    def test_features_config_keeps_file_cmd_prefixes_raw(self, tmp_path):
        """file:/cmd: inside features_config values are NOT resolved at
        construction — they stay raw so resolve_dynamic_config can deref
        them per-key (consistent with the env path, which is also
        non-fail-fast)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "features_config:\n"
            '  KLANGKWS_FEATURE_TOKEN: "file:/run/secrets/token"\n'
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.features_config == {
            "KLANGKWS_FEATURE_TOKEN": "file:/run/secrets/token"
        }

    def test_features_config_defaults_none(self):
        # No block in the file (or no file) → None, and resolve_dynamic_config
        # sees no second source.
        s = make_settings({}, config_file="none")
        assert s.features_config is None


# ---------------------------------------------------------------------------
# Dual-form keys: kebab-case *and* snake_case (config-file style, #1538)
# ---------------------------------------------------------------------------


class TestDualFormKeys:
    """Every config-file key may be written in either snake_case or
    kebab-case and resolve to the same field (#1538). snake_case remains the
    documented/preferred form; kebab-case is accepted for backwards compat
    and consistency with the wider config-file style (e.g. klangk.yaml, OIDC
    provider dicts). Top-level keys are normalized by
    ``_KebabYamlConfigSettingsSource``; nested OIDC provider dicts are
    handled separately by :func:`klangk.oidc.get`."""

    def test_kebab_case_key_loads(self, tmp_path):
        """A hyphenated top-level key maps to its snake_case field."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('egress-port: "9999"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.egress_port == "9999"

    def test_snake_case_key_loads(self, tmp_path):
        """snake_case (the documented form) still loads unchanged."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('egress_port: "7777"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.egress_port == "7777"

    def test_kebab_and_snake_resolve_same_field(self, tmp_path):
        """Both forms populate the same field (not two different ones)."""
        cfg_kebab = tmp_path / "kebab.yaml"
        cfg_kebab.write_text('brand-color: "#111111"\n')
        cfg_snake = tmp_path / "snake.yaml"
        cfg_snake.write_text('brand_color: "#222222"\n')
        s_kebab = make_settings({}, config_file=str(cfg_kebab))
        s_snake = make_settings({}, config_file=str(cfg_snake))
        assert s_kebab.brand_color == "#111111"
        assert s_snake.brand_color == "#222222"

    def test_multi_word_kebab_keys(self, tmp_path):
        """Several multi-word keys accept kebab-case in one file."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            'product-name: "Kebab"\n'
            'trusted-proxy-cidrs: "10.0.0.0/8"\n'
            'login-lockout-window: "600"\n'
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.product_name == "Kebab"
        assert s.trusted_proxy_cidrs == "10.0.0.0/8"
        assert s.login_lockout_window == 600

    def test_kebab_required_dir(self, tmp_path):
        """state-dir (kebab) satisfies the required-dir validator."""
        cfg = tmp_path / "config.yaml"
        state = tmp_path / "state"
        state.mkdir()
        cfg.write_text(f'state-dir: "{state}"\n')
        # Direct construction: env has no STATE_DIR, so the kebab key in the
        # config file is the sole source (make_settings would inject one).
        s = KlangkSettings(
            env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
            config_file=str(cfg),
        )
        assert s.state_dir == str(state)

    def test_nested_oidc_providers_not_normalized(self, tmp_path):
        """Nested dicts (oidc_providers entries) are left verbatim — their
        dual-form lookup is handled by oidc.get(), not the YAML source."""

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "oidc_providers:\n"
            "  - id: cac\n"
            "    client-id: klangk\n"
            "    client-secret: sekret\n"
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.oidc_providers == [
            {"id": "cac", "client-id": "klangk", "client-secret": "sekret"}
        ]


# ---------------------------------------------------------------------------
# _resolve_socket_and_ports validator (listen-shape settings, #1542)
# ---------------------------------------------------------------------------
# KLANGKD_PORT (unset ⇒ headless, set ⇒ browser), KLANGKD_EGRESS_PORT (container
# egress), KLANGKD_SOCKET (backend UDS), and the deprecated KLANGKD_PROXY_PORT
# alias are resolved once at construction. Callers read ``egress_port`` /
# ``socket`` only; ``proxy_port`` is a deprecated alias folded into
# ``egress_port`` (egress-wins) and slated for removal.


class TestResolveSocketAndPorts:
    def test_socket_defaults_to_state_dir_klangk_sock(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.socket == os.path.join("/tmp/state", "klangk.sock")

    def test_explicit_socket_wins(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_SOCKET": "/short/klangk.sock",
            }
        )
        assert s.socket == "/short/klangk.sock"

    def test_egress_port_defaults_to_8995(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.egress_port == "8995"

    def test_explicit_egress_port_wins(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_EGRESS_PORT": "7777",
            }
        )
        assert s.egress_port == "7777"

    def test_port_defaults_to_none_headless(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.port is None

    def test_listen_defaults_to_loopback(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.listen == "127.0.0.1"

    def test_egress_listen_defaults_to_all_interfaces(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.egress_listen == "0.0.0.0"

    def test_egress_listen_override(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_EGRESS_LISTEN": "192.168.1.5",
            }
        )
        assert s.egress_listen == "192.168.1.5"

    def test_proxy_port_folded_into_egress_with_warning(self, caplog):
        """KLANGKD_PROXY_PORT alone (no egress) is used as egress + a deprecation warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            s = KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_PROXY_PORT": "9999",
                }
            )
        assert s.egress_port == "9999"
        assert any(
            "KLANGKD_PROXY_PORT is deprecated" in r.message
            for r in caplog.records
        )

    def test_egress_wins_over_proxy_port_with_warning(self, caplog):
        """Both set: egress_port wins, proxy_port ignored + a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            s = KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_EGRESS_PORT": "8995",
                    "KLANGKD_PROXY_PORT": "9999",
                }
            )
        assert s.egress_port == "8995"
        assert any(
            "KLANGKD_PROXY_PORT is ignored" in r.message
            for r in caplog.records
        )

    def test_egress_equals_port_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_PORT": "8995",
                    "KLANGKD_EGRESS_PORT": "8995",
                }
            )

    def test_socket_too_long_rejected(self):
        from pydantic import ValidationError

        # Build a path over 104 chars by setting a very long socket directly.
        long_socket = "/" + "a" * 104 + ".sock"
        assert len(long_socket) > 104
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_SOCKET": long_socket,
                }
            )
        msg = str(exc_info.value)
        assert "KLANGKD_SOCKET" in msg
        assert "#1531" in msg

    def test_socket_length_error_directs_to_state_dir_or_socket(self):
        from pydantic import ValidationError

        long_socket = "/" + "a" * 104 + ".sock"
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_SOCKET": long_socket,
                }
            )
        msg = str(exc_info.value)
        assert "KLANGKD_STATE_DIR" in msg
        assert "KLANGKD_SOCKET" in msg

    # --- Caddy admin socket (#1636) ---
    # Mirrors the backend-socket field's default + override + length guard.
    # The admin UDS is only bound under the Caddy engine, but the length check
    # fires at construction regardless of engine so a deep state_dir fails fast
    # with a named-var diagnostic instead of cryptically in _wait_for_admin.

    def test_caddy_admin_socket_defaults_to_state_dir(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.caddy_admin_socket == os.path.join(
            "/tmp/state", "caddy-admin.sock"
        )

    def test_explicit_caddy_admin_socket_wins(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_CADDY_ADMIN_SOCKET": "/short/caddy-admin.sock",
            }
        )
        assert s.caddy_admin_socket == "/short/caddy-admin.sock"

    def test_caddy_admin_socket_too_long_rejected(self):
        from pydantic import ValidationError

        long_admin = "/" + "a" * 104 + ".sock"
        assert len(long_admin) > 104
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_CADDY_ADMIN_SOCKET": long_admin,
                    # Keep the backend socket short so the ONLY failure is the
                    # admin socket — proves the check is per-field.
                    "KLANGKD_SOCKET": "/short/klangk.sock",
                }
            )
        msg = str(exc_info.value)
        assert "KLANGKD_CADDY_ADMIN_SOCKET" in msg
        assert "#1636" in msg

    def test_caddy_admin_socket_error_names_the_var(self):
        """The diagnostic names KLANGKD_CADDY_ADMIN_SOCKET (not just state_dir)
        so the operator can fix the right socket when only one is too long."""
        from pydantic import ValidationError

        long_admin = "/" + "a" * 104 + ".sock"
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_CADDY_ADMIN_SOCKET": long_admin,
                    "KLANGKD_SOCKET": "/short/klangk.sock",
                }
            )
        msg = str(exc_info.value)
        assert "KLANGKD_CADDY_ADMIN_SOCKET" in msg


class TestKlangkdLauncher:
    """Tests for the klangkd launcher's --config resolution."""

    def test_resolve_config_path_existing(self, tmp_path):
        from klangk.launcher import _resolve_config_path

        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: test\n")
        assert _resolve_config_path(str(cfg)) == str(cfg)

    def test_resolve_config_path_none(self):
        from klangk.launcher import _resolve_config_path

        assert _resolve_config_path("none") == "none"

    def test_resolve_config_path_missing(self):
        import pytest as _pytest
        from klangk.launcher import _resolve_config_path
        import typer

        with _pytest.raises(typer.BadParameter):
            _resolve_config_path("/nonexistent/path/to/config.yaml")


class TestEnvConstructor:
    """Tests for the make_settings(...) constructor (#1426 Slice 1)."""

    def test_reads_from_env_dict(self):
        # Explicit env dict is the only source — os.environ is ignored.
        s = make_settings({"KLANGKD_EGRESS_PORT": "4321"})
        assert s.egress_port == "4321"

    def test_env_dict_ignores_os_environ(self, monkeypatch):
        monkeypatch.setenv("KLANGKD_EGRESS_PORT", "9999")
        s = make_settings({"KLANGKD_EGRESS_PORT": "1111"})
        assert s.egress_port == "1111"
        assert s.egress_port != "9999"

    def test_empty_env_dict_uses_defaults(self):
        import getpass

        s = make_settings({})
        assert s.auth_modes is None
        # default_user is derived from the invoking Unix user (#1645).
        assert s.default_user == f"{getpass.getuser()}@example.com"
        assert s.min_password_length == 8

    def test_default_user_falls_back_when_getuser_fails(self, monkeypatch):
        # In containers/CI where the uid has no passwd entry, getpass.getuser()
        # raises OSError (since Python 3.13; earlier versions raised KeyError
        # / ImportError, but the wrapper normalizes now) — the default must
        # fall back to "user" so construction doesn't crash (#1645).
        import getpass as getpass_mod

        def _raise():
            raise OSError("No username set in the environment")

        monkeypatch.setattr(getpass_mod, "getuser", _raise)
        s = make_settings({})
        assert s.default_user == "user@example.com"

    def test_env_for_sources_reset_after_construction(self):
        # The class-var bridge is cleaned up after construction so it doesn't
        # leak between instances.
        make_settings({"KLANGKD_EGRESS_PORT": "1234"})
        assert KlangkSettings._env_for_sources is None

    def test_env_dict_multiple_fields(self):
        s = make_settings(
            env={
                "KLANGKD_AUTH_MODES": "password",
                "KLANGKD_JWT_SECRET": "secret123",
                "KLANGKD_DEFAULT_USER": "admin@test.com",
            }
        )
        assert s.auth_modes == "password"
        assert s.jwt_secret == "secret123"
        assert s.default_user == "admin@test.com"

    def test_config_file_param_loads_yaml(self, tmp_path):
        # The config_file= constructor param wires a YAML source in.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: FromConfigFile\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.product_name == "FromConfigFile"

    def test_env_overrides_config_file(self, tmp_path):
        # Precedence: env dict > config file.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: FromConfigFile\n")
        s = make_settings(
            env={"KLANGKD_PRODUCT_NAME": "FromEnv"}, config_file=str(cfg)
        )
        assert s.product_name == "FromEnv"


class TestAuthModesValidator:
    """KLANGKD_AUTH_MODES is security-sensitive: a typo must fail at
    construction (boot), not silently downgrade to the no-auth ``none`` mode
    (which freely issues an admin token)."""

    @pytest.mark.parametrize("mode", ["password", "oidc", "both", "none"])
    def test_valid_modes_accepted(self, mode):
        s = make_settings({"KLANGKD_AUTH_MODES": mode})
        assert s.auth_modes == mode

    def test_unset_allowed_means_none(self):
        # None = unset = "default to none at read time" (legitimate).
        s = make_settings({})
        assert s.auth_modes is None

    @pytest.mark.parametrize(
        "bad", ["passdword", "PASSWORD", " true", "x", "None"]
    )
    def test_typo_rejected_at_construction(self, bad):
        # A set-but-garbage value must raise (not silently become "none").
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUTH_MODES": bad})

    def test_empty_string_treated_as_unset(self):
        # KLANGKD_AUTH_MODES="" (set but blank) is treated as unset → None →
        # "none" at read time, preserving the pre-validator behavior.
        # (Not a security risk: blank is a config mistake, not a typo'd
        # secure-mode name silently degrading.)
        s = make_settings({"KLANGKD_AUTH_MODES": ""})
        assert s.auth_modes is None

    def test_typo_error_message_lists_valid_modes(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings({"KLANGKD_AUTH_MODES": "passdword"})
        msg = str(exc_info.value)
        assert "passdword" in msg
        assert "password" in msg  # valid modes listed in the message


class TestLogLevelValidator:
    """KLANGKD_LOG_LEVEL must be a recognized level or fail fast at boot
    (#1467), mirroring the fail-fast posture of the auth_modes validator."""

    def test_defaults_to_info(self):
        s = make_settings({})
        assert s.log_level == "INFO"

    @pytest.mark.parametrize(
        "name", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    )
    def test_valid_names_accepted_any_case(self, name):
        # lower, upper, mixed all normalize to upper
        s = make_settings({"KLANGKD_LOG_LEVEL": name.lower()})
        assert s.log_level == name

    @pytest.mark.parametrize("num", ["0", "10", "20", "30", "40", "50"])
    def test_numeric_string_accepted(self, num):
        s = make_settings({"KLANGKD_LOG_LEVEL": num})
        assert s.log_level == num

    def test_empty_string_defaults_to_info(self):
        s = make_settings({"KLANGKD_LOG_LEVEL": ""})
        assert s.log_level == "INFO"

    @pytest.mark.parametrize(
        "bad", ["verbose", "TRACE", "info!", "debug-level"]
    )
    def test_garbage_rejected_at_construction(self, bad):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_LOG_LEVEL": bad})

    def test_error_message_names_valid_levels(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings({"KLANGKD_LOG_LEVEL": "verbose"})
        msg = str(exc_info.value)
        assert "verbose" in msg
        assert "DEBUG" in msg  # valid levels listed in the message


class TestResolveIndirectionsValidator:
    """The ``_resolve_indirections`` model validator runs once at construction
    (#1461): every string field with a ``file:``/``cmd:`` prefix is resolved
    before the object leaves ``__init__``. Thereafter ``settings.field`` is
    the resolved value — no caller wraps in ``resolve_indirection``. A bad
    reference fails fast at construction (boot), not silently at use time."""

    def test_file_resolved_at_construction(self, tmp_path):
        secret = tmp_path / "jwt.txt"
        secret.write_text("the-real-secret\n")
        s = make_settings({"KLANGKD_JWT_SECRET": f"file:{secret}"})
        assert s.jwt_secret == "the-real-secret"

    def test_cmd_resolved_at_construction(self):
        s = make_settings(
            env={"KLANGKD_JWT_SECRET": "cmd:printf %s cmd-secret"}
        )
        assert s.jwt_secret == "cmd-secret"

    def test_plain_value_passes_through(self):
        s = make_settings({"KLANGKD_JWT_SECRET": "plain-secret"})
        assert s.jwt_secret == "plain-secret"

    def test_none_field_left_alone(self):
        # Unset fields stay None (not passed through the resolver — the
        # isinstance(val, str) guard skips them).
        s = make_settings({})
        assert s.smtp_password is None

    def test_file_missing_fails_at_construction(self):
        # fail-fast: a dangling file: reference aborts boot, not silent None.
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings({"KLANGKD_JWT_SECRET": "file:/nonexistent/path"})
        msg = str(exc_info.value)
        assert "JWT_SECRET" in msg

    def test_cmd_failure_fails_at_construction(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_JWT_SECRET": "cmd:false"})

    def test_idempotent_re_resolution(self):
        # A plain (already-resolved) value survives a second pass unchanged —
        # the legacy resolve_env_value path reads the resolved field and its
        # redundant _resolve_indirection call is a no-op.
        s = make_settings({"KLANGKD_EGRESS_PORT": "8995"})
        assert _resolve_indirection(s.egress_port) == "8995"

    def test_non_string_field_skipped(self):
        # oidc_providers is list[dict] | None — not a str, skipped by the
        # validator (would crash if isinstance check were missing).
        s = make_settings({"KLANGKD_OIDC_PROVIDERS": '[{"name": "x"}]'})
        assert s.oidc_providers == [{"name": "x"}]


class TestRequireDirsValidator:
    """``state_dir`` defaults to ``$XDG_STATE_HOME/klangk`` (→
    ``~/.local/state/klangk``) when unset (#1644); ``data_dir`` derives from
    ``state_dir``; ``customize_dir`` defaults to ``<config_dir>/custom`` via
    the new ``config_dir`` root (config, not state — #1644/#1649).
    ``plugins_dir`` is gone from settings entirely (#1655) — the runtime
    reads the build-emitted ``features.json`` from ``frontend_dir``.
    Explicit values win. The #1461 fail-fast intent is preserved only for the
    pathological case where no home can be computed ($HOME unset)."""

    def test_state_dir_defaults_to_xdg_state_home(self, monkeypatch):
        # Pin both XDG roots + HOME so the default is deterministic and
        # doesn't leak the developer's real home into the assertion.
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xstate")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xcfg")
        monkeypatch.setenv("HOME", "/tmp/fakehome")
        s = KlangkSettings(env={})
        assert s.state_dir == os.path.join("/tmp/xstate", "klangkd")

    def test_state_dir_defaults_to_home_when_xdg_unset(self, monkeypatch):
        # The documented XDG fallback: unset var → ~/.local/state.
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HOME", "/tmp/fakehome")
        s = KlangkSettings(env={})
        assert s.state_dir == os.path.join(
            "/tmp/fakehome", ".local", "state", "klangkd"
        )

    def test_explicit_state_dir_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xstate")
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/explicit/state"})
        assert s.state_dir == "/explicit/state"

    def test_state_dir_still_required_when_home_unset(self, monkeypatch):
        """The #1461 fail-fast intent survives — but only for the genuinely
        unconfigured case where no home path can be computed ($HOME unset and
        $XDG_STATE_HOME unset). A sensible default exists whenever $HOME does."""
        from pydantic import ValidationError

        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(env={})
        assert "KLANGKD_STATE_DIR" in str(exc_info.value)

    def test_data_dir_defaults_to_state_dir_data(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.data_dir == os.path.join("/tmp/state", "data")

    def test_explicit_data_dir_wins(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_DATA_DIR": "/explicit/data",
            }
        )
        assert s.data_dir == "/explicit/data"

    def test_plugins_dir_removed_from_settings(self):
        # plugins_dir is gone from KlangkSettings entirely (#1655) — the
        # runtime reads the build-emitted features.json from frontend_dir.
        # The build materializes features into a tempdir (#1660); there is no
        # KLANGKD_PLUGINS_DIR env var at any layer.
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert not hasattr(s, "plugins_dir")

    def test_klangk_plugins_dir_env_not_recognized(self):
        # KLANGKD_PLUGINS_DIR does not exist as a concept anywhere (#1660 —
        # dropped from the build too). pydantic-settings ignores unknown env
        # keys (no error), and the resulting settings object has no
        # plugins_dir attribute — the var simply has no effect.
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_PLUGINS_DIR": "/explicit/plugins",
            }
        )
        assert not hasattr(s, "plugins_dir")
        # The var didn't get picked up as anything else either.
        assert s.state_dir == "/tmp/state"

    def test_customize_dir_defaults_to_xdg_config_home(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xcfg")
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.customize_dir == os.path.join(
            "/tmp/xcfg", "klangkd", "custom"
        )

    def test_explicit_customize_dir_wins(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_CUSTOMIZE_DIR": "/explicit/custom",
            }
        )
        assert s.customize_dir == "/explicit/custom"

    def test_config_dir_and_customize_default_under_config_home(
        self, monkeypatch
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xcfg")
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xstate")
        s = KlangkSettings(env={})
        # state tree (derived from state_dir): data_dir only — plugins_dir is
        # gone (#1655).
        assert s.state_dir == os.path.join("/tmp/xstate", "klangkd")
        assert s.data_dir == os.path.join("/tmp/xstate", "klangkd", "data")
        # config tree root (#1649) + its derived customize_dir.
        assert s.config_dir == os.path.join("/tmp/xcfg", "klangkd")
        assert s.customize_dir == os.path.join(
            "/tmp/xcfg", "klangkd", "custom"
        )

    # --- config_dir: the config-tree root (#1649) ---

    def test_config_dir_defaults_to_xdg_config_home(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xcfg")
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.config_dir == os.path.join("/tmp/xcfg", "klangkd")

    def test_config_dir_defaults_to_home_when_xdg_unset(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", "/tmp/fakehome")
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.config_dir == os.path.join(
            "/tmp/fakehome", ".config", "klangkd"
        )

    def test_explicit_config_dir_wins_and_propagates(self):
        # An explicit KLANGKD_CONFIG_DIR overrides the XDG default AND
        # customize_dir derives from it (the single-knob relocation point).
        # plugins_dir is gone (#1655) — nothing to check there.
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_CONFIG_DIR": "/my/cfg",
            }
        )
        assert s.config_dir == "/my/cfg"
        assert s.customize_dir == os.path.join("/my/cfg", "custom")

    def test_customize_override_wins_over_config_dir_derivation(self):
        # KLANGKD_CUSTOMIZE_DIR still wins over the config_dir-derived default.
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_CONFIG_DIR": "/my/cfg",
                "KLANGKD_CUSTOMIZE_DIR": "/explicit/custom",
            }
        )
        assert s.config_dir == "/my/cfg"
        assert s.customize_dir == "/explicit/custom"

    # --- features_enable: per-deploy activation (#1655) ---

    def test_features_enable_defaults_to_none(self):
        s = KlangkSettings(env={"KLANGKD_STATE_DIR": "/tmp/state"})
        assert s.features_enable is None

    def test_features_enable_explicit_list(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_FEATURES_ENABLE": "celebrate,beep,soliplex",
            }
        )
        # Canonical semantics: the value is carried verbatim (no parsing,
        # no `*` expansion — the frontend resolves it against features.json).
        assert s.features_enable == "celebrate,beep,soliplex"

    def test_features_enable_single_value(self):
        s = KlangkSettings(
            env={
                "KLANGKD_STATE_DIR": "/tmp/state",
                "KLANGKD_FEATURES_ENABLE": "soliplex",
            }
        )
        assert s.features_enable == "soliplex"


class TestReload:
    """KlangkSettings.reload() re-resolves from the same sources (#1587)."""

    def test_reload_returns_fresh_instance(self):
        s = make_settings({"KLANGKD_DEFAULT_USER": "bot@example.com"})
        s2 = s.reload()
        assert s2 is not s
        assert s2.default_user == "bot@example.com"

    def test_reload_picks_up_changed_env(self):
        env = {
            "KLANGKD_DATA_DIR": "/d",
            "KLANGKD_STATE_DIR": "/s",
            "KLANGKD_DEFAULT_USER": "old@example.com",
        }
        s = KlangkSettings(env)
        env["KLANGKD_DEFAULT_USER"] = "new@example.com"
        s2 = s.reload()
        assert s2.default_user == "new@example.com"
        assert s.default_user == "old@example.com"

    def test_reload_picks_up_features_config_block(self, tmp_path):
        # The changelog/user-facing docs claim features_config: is read at
        # boot AND on SIGHUP (reloadable). reload() re-reads the same
        # config file captured at construction, so editing the block between
        # constructions must surface on the reloaded instance. Verify the
        # claim directly rather than relying on the generic mechanism.
        cfg = tmp_path / "klangkd.yaml"
        cfg.write_text('features_config:\n  KLANGKWS_FEATURE_X: "old"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.features_config == {"KLANGKWS_FEATURE_X": "old"}
        # Operator edits the block (SIGHUP path).
        cfg.write_text(
            "features_config:\n"
            '  KLANGKWS_FEATURE_X: "new"\n'
            '  KLANGKWS_FEATURE_Y: "added"\n'
        )
        s2 = s.reload()
        assert s2.features_config == {
            "KLANGKWS_FEATURE_X": "new",
            "KLANGKWS_FEATURE_Y": "added",
        }
        # The pre-reload instance is unchanged (reload returns a fresh obj).
        assert s.features_config == {"KLANGKWS_FEATURE_X": "old"}

    def test_reload_raises_on_invalid_config(self):
        s = make_settings({})
        with pytest.raises(Exception):
            # auth_modes must be a valid value; "bogus" will fail validation.
            env = dict(s._reload_env)
            env["KLANGKD_AUTH_MODES"] = "bogus"
            KlangkSettings(env)


class TestLLMModelsValidator:
    """Tests for the KLANGKD_LLM_MODELS field validator (#2070)."""

    def test_none_is_disabled(self):
        s = make_settings({})
        assert s.llm_models is None

    def test_empty_string_is_disabled(self):
        s = make_settings({"KLANGKD_LLM_MODELS": ""})
        assert s.llm_models is None

    def test_comma_separated_string(self):
        s = make_settings(
            {
                "KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx,ollama/llama3:http://x:11434:"
            }
        )
        assert s.llm_models is not None
        assert len(s.llm_models) == 2

    def test_list_from_yaml(self, tmp_path):
        cfg = tmp_path / "klangkd.yaml"
        cfg.write_text(
            "llm-models:\n"
            "  - 'openai/gpt-4o::sk-xxx'\n"
            "  - 'ollama/llama3:http://x:11434:'\n"
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.llm_models is not None
        assert len(s.llm_models) == 2

    def test_dict_entries_from_yaml(self, tmp_path):
        cfg = tmp_path / "klangkd.yaml"
        cfg.write_text(
            "llm-models:\n"
            "  - model_name: gpt-4\n"
            "    litellm_params:\n"
            "      model: openai/gpt-4o\n"
            "      api_key: sk-xxx\n"
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.llm_models is not None
        assert len(s.llm_models) == 1
        assert isinstance(s.llm_models[0], dict)

    def test_empty_list_is_disabled(self, tmp_path):
        cfg = tmp_path / "klangkd.yaml"
        cfg.write_text("llm-models: []\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.llm_models is None

    def test_invalid_string_entry_raises(self):
        with pytest.raises(Exception, match="two colons"):
            make_settings({"KLANGKD_LLM_MODELS": "openai/gpt-4o"})


class TestNixSeedConfig:
    """nix_seed: {type, path} — the repo's first nested settings model (#2220)."""

    def test_bogus_type_rejected(self):
        """An invalid nix_seed.type aborts construction (the Literal enum)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_NIX_SEED__TYPE": "zfs"})

    def test_yaml_block_form(self, tmp_path):
        """The nix_seed: {type, path} YAML block parses (nested model)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("nix_seed:\n  type: btrfs-snapshot\n  path: /seed\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.nix_seed.type == "btrfs-snapshot"
        assert s.nix_seed.path == "/seed"

    def test_disabled_when_omitted(self):
        """Omitting nix_seed entirely -> not configured (image-only)."""
        s = make_settings({})
        assert s.nix_seed.path is None
        assert s.nix_seed.type == "fuse-overlayfs"  # the default


# ---------------------------------------------------------------------------
# egress_consent prune knobs (#2303)
# ---------------------------------------------------------------------------


class TestEgressConsentPruneSettings:
    def test_defaults(self):
        s = make_settings({})
        assert s.egress_consent_retention_days == 30
        assert s.egress_consent_row_cap == 2000

    def test_env_overrides(self):
        s = make_settings(
            {
                "KLANGKD_EGRESS_CONSENT_RETENTION_DAYS": "7",
                "KLANGKD_EGRESS_CONSENT_ROW_CAP": "500",
            }
        )
        assert s.egress_consent_retention_days == 7
        assert s.egress_consent_row_cap == 500

    def test_zero_disables(self):
        s = make_settings(
            {
                "KLANGKD_EGRESS_CONSENT_RETENTION_DAYS": "0",
                "KLANGKD_EGRESS_CONSENT_ROW_CAP": "0",
            }
        )
        assert s.egress_consent_retention_days == 0
        assert s.egress_consent_row_cap == 0

    def test_empty_string_falls_back_to_default(self):
        s = make_settings(
            {
                "KLANGKD_EGRESS_CONSENT_RETENTION_DAYS": "",
                "KLANGKD_EGRESS_CONSENT_ROW_CAP": "",
            }
        )
        assert s.egress_consent_retention_days == 30
        assert s.egress_consent_row_cap == 2000

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("KLANGKD_EGRESS_CONSENT_RETENTION_DAYS", "-1"),
            ("KLANGKD_EGRESS_CONSENT_ROW_CAP", "-5"),
            ("KLANGKD_EGRESS_CONSENT_RETENTION_DAYS", "soon"),
            ("KLANGKD_EGRESS_CONSENT_ROW_CAP", "1.5"),
        ],
    )
    def test_malformed_raises(self, key, value):
        with pytest.raises(Exception, match=key):
            make_settings({key: value})

    def test_env_string_float_rejected(self):
        with pytest.raises(Exception, match="RETENTION_DAYS"):
            make_settings({"KLANGKD_EGRESS_CONSENT_RETENTION_DAYS": "0.5"})

    @pytest.mark.parametrize("value", [0.5, True])
    def test_native_yaml_float_and_bool_rejected(self, value, tmp_path):
        # A YAML float (retention-days: 0.5) must abort rather than truncate
        # (int(0.5) == 0 would silently disable the feature); a YAML bool
        # (true -> 1) is a typo, not a window.
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"egress-consent-retention-days: {value!s}\n")
        with pytest.raises(Exception, match="RETENTION_DAYS"):
            make_settings({}, config_file=str(cfg))


class TestNumericSettingCoercion:
    """Numeric settings accept int/float, string, and file:/cmd: (#2603).

    Every field must accept all three source forms: a bare YAML number
    (``min-password-length: 12`` parses as an int and used to fail the
    str-typed field), a quoted string / env string, and a ``file:`` or
    ``cmd:`` reference (legal while the fields were str-typed; the
    indirection is resolved before coercion).
    """

    INT_FIELDS = [
        "min_password_length",
        "login_lockout_failures",
        "login_lockout_duration",
        "login_lockout_window",
        "max_sessions_per_user",
        "invite_expire_hours",
        "port_range_start",
        "websocket_msg_size_max",
        "file_upload_size_max",
        "hosted_ports_per_workspace",
        "smtp_port",
        "password_history_count",
    ]
    # 0 is legal here (disable semantics), so only these get the 0-rejection
    INT_NO_ZERO_FIELDS = [
        "invite_expire_hours",
        "port_range_start",
        "websocket_msg_size_max",
        "file_upload_size_max",
        "smtp_port",
    ]
    FLOAT_FIELDS = [
        "access_token_hours",
        "workspace_token_hours",
        "health_check_interval",
        "health_check_timeout",
        "health_check_startup_grace",
        "quiesce_timeout",
    ]

    @pytest.mark.parametrize(
        "field,bad",
        [(f, True) for f in INT_FIELDS]
        + [(f, "abc") for f in INT_FIELDS]
        + [(f, 1.5) for f in INT_FIELDS]
        + [(f, -1) for f in INT_FIELDS]
        + [
            # 0 is rejected where it has no zero-semantics (empty port
            # range, 0-byte uploads, instantly-expiring invites). The
            # documented-disable fields (length floor, lockout trio,
            # hosted ports) are asserted separately below.
            (f, 0)
            for f in INT_NO_ZERO_FIELDS
        ]
        + [("smtp_port", 70000)],
    )
    def test_int_field_rejections(self, field, bad):
        with pytest.raises(Exception, match=field):
            make_settings({f"KLANGKD_{field.upper()}": str(bad)})

    def test_port_range_start_above_last_port_rejected(self):
        with pytest.raises(Exception, match="port_range_start"):
            make_settings({"KLANGKD_PORT_RANGE_START": "70000"})

    def test_password_history_count_capped(self):
        """#2582: the reuse window is capped (each retired hash costs a
        PBKDF2 verify per set); 24 is the documented maximum."""
        with pytest.raises(Exception, match="password_history_count"):
            make_settings({"KLANGKD_PASSWORD_HISTORY_COUNT": "25"})
        s = make_settings({"KLANGKD_PASSWORD_HISTORY_COUNT": "24"})
        assert s.password_history_count == 24

    @pytest.mark.parametrize(
        "field",
        [
            "min_password_length",
            "login_lockout_failures",
            "login_lockout_duration",
            "login_lockout_window",
            "max_sessions_per_user",
            "hosted_ports_per_workspace",
            "password_history_count",
        ],
    )
    def test_zero_keeps_disable_semantics(self, field):
        """0 stays legal where the code treats it as "off" (length floor,
        lockout) — tightening to >= 1 would silently re-enable controls an
        operator deliberately disabled."""
        s = make_settings({f"KLANGKD_{field.upper()}": "0"})
        assert getattr(s, field) == 0

    @pytest.mark.parametrize(
        "field,bad",
        [(f, True) for f in FLOAT_FIELDS]
        + [(f, "abc") for f in FLOAT_FIELDS]
        + [(f, -1) for f in FLOAT_FIELDS],
    )
    def test_float_field_rejections(self, field, bad):
        with pytest.raises(Exception, match=field):
            make_settings({f"KLANGKD_{field.upper()}": str(bad)})

    def test_yaml_bare_numbers_accepted(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "min-password-length: 12\n"  # kebab + bare int
            "login_lockout_window: 600\n"
            "access-token-hours: 48\n"  # bare int for a float field
            "health-check-interval: 15.5\n"  # bare float
            "smtp_port: 25\n"
            "smtp-use-tls: false\n"  # native bool for the tls toggle
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.min_password_length == 12
        assert s.login_lockout_window == 600
        assert s.access_token_hours == 48.0
        assert s.health_check_interval == 15.5
        assert s.smtp_port == 25
        assert s.smtp_use_tls == "false"

    @pytest.mark.parametrize(
        "yaml_value",
        ["true", "1.5"],  # native YAML bool / float for an int field
    )
    def test_yaml_native_bool_and_float_rejected(self, yaml_value, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"min-password-length: {yaml_value}\n")
        with pytest.raises(Exception, match="min_password_length"):
            make_settings({}, config_file=str(cfg))

    def test_yaml_native_bool_rejected_for_float_field(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("access-token-hours: true\n")
        with pytest.raises(Exception, match="access_token_hours"):
            make_settings({}, config_file=str(cfg))

    def test_yaml_quoted_strings_still_accepted(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            'min-password-length: "12"\naccess_token_hours: "1.5"\n'
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.min_password_length == 12
        assert s.access_token_hours == 1.5

    def test_env_strings_still_accepted(self):
        s = make_settings(
            {
                "KLANGKD_MIN_PASSWORD_LENGTH": "10",
                "KLANGKD_ACCESS_TOKEN_HOURS": "1.5",
                "KLANGKD_SMTP_PORT": "2525",
            }
        )
        assert s.min_password_length == 10
        assert s.access_token_hours == 1.5
        assert s.smtp_port == 2525

    def test_file_indirection_still_works(self, tmp_path):
        secret = tmp_path / "port"
        secret.write_text("2525\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"smtp_port: file:{secret}\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.smtp_port == 2525

    def test_file_indirection_failure_fails_fast(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("smtp_port: file:/nonexistent/definitely-missing\n")
        with pytest.raises(Exception, match="smtp_port"):
            make_settings({}, config_file=str(cfg))

    def test_defaults_unchanged(self):
        s = make_settings({})
        assert s.min_password_length == 8
        assert s.login_lockout_failures == 5
        assert s.login_lockout_duration == 900
        assert s.login_lockout_window == 300
        assert s.max_sessions_per_user == 0
        assert s.inactivity_disable_days == 35
        assert s.invite_expire_hours == 72
        assert s.access_token_hours == 24.0
        assert s.workspace_token_hours == 24.0
        assert s.port_range_start == 9000
        assert s.websocket_msg_size_max == 16777216
        assert s.smtp_port == 587
        assert s.file_upload_size_max == 524288000
        assert s.health_check_interval is None
        assert s.health_check_timeout is None
        assert s.hosted_ports_per_workspace == 5

    @pytest.mark.parametrize(
        "field,default",
        [
            ("min_password_length", 8),
            ("login_lockout_failures", 5),
            ("login_lockout_duration", 900),
            ("login_lockout_window", 300),
            ("max_sessions_per_user", 0),
            ("inactivity_disable_days", 35),
            ("invite_expire_hours", 72),
            ("port_range_start", 9000),
            ("websocket_msg_size_max", 16777216),
            ("smtp_port", 587),
            ("file_upload_size_max", 524288000),
            ("hosted_ports_per_workspace", 5),
            ("access_token_hours", 24.0),
            ("workspace_token_hours", 24.0),
        ],
    )
    def test_empty_env_falls_back_to_field_default(self, field, default):
        """Empty/None -> the declared default, never None (#2605 review).

        Consumers assume a number on these fields (``len(pw) < None`` would
        500 /api/config pre-auth; ``int(None)`` crashes emailsvc, the
        upload check, and the launcher). Empty-as-disable is expressed by
        the explicit 0, not by unsetting the field.
        """
        s = make_settings({f"KLANGKD_{field.upper()}": ""})
        assert getattr(s, field) == default

    @pytest.mark.parametrize(
        "field",
        [
            "health_check_interval",
            "health_check_timeout",
            "health_check_startup_grace",
        ],
    )
    def test_empty_env_stays_none_for_optional_floats(self, field):
        # The health_check_* trio is genuinely optional (None = the
        # consumer-side 30/10/30 defaults); empty keeps that meaning.
        s = make_settings({f"KLANGKD_{field.upper()}": ""})
        assert getattr(s, field) is None

    @pytest.mark.parametrize("value", ["true", "false", True, False])
    def test_smtp_use_tls_accepts_bool_and_string(self, value, tmp_path):
        if isinstance(value, bool):
            cfg = tmp_path / "config.yaml"
            cfg.write_text(f"smtp-use-tls: {str(value).lower()}\n")
            s = make_settings({}, config_file=str(cfg))
            assert s.smtp_use_tls == str(value).lower()
        else:
            s = make_settings({"KLANGKD_SMTP_USE_TLS": value})
            assert s.smtp_use_tls == value


class TestPasswordRequireCounts:
    """KLANGKD_PASSWORD_REQUIRE_* coercion (#2581).

    Ints (YAML), integer strings (env), None/empty (-> 0) are accepted;
    floats, bools, negatives, and non-integers abort startup.
    """

    def test_env_integer_strings(self):
        s = make_settings(
            {
                "KLANGKD_PASSWORD_REQUIRE_UPPER": "2",
                "KLANGKD_PASSWORD_REQUIRE_DIGIT": "1",
            }
        )
        assert s.password_require_upper == 2
        assert s.password_require_digit == 1
        assert s.password_require_lower == 0

    def test_env_empty_string_is_zero(self):
        s = make_settings({"KLANGKD_PASSWORD_REQUIRE_UPPER": ""})
        assert s.password_require_upper == 0

    def test_defaults_are_zero(self):
        s = make_settings({})
        assert s.password_require_upper == 0
        assert s.password_require_lower == 0
        assert s.password_require_digit == 0
        assert s.password_require_special == 0

    def test_yaml_native_ints(self, tmp_path):
        # The reported bug (#2602 review): ``password_require_upper: 2``
        # parses as an int from YAML and must not hit the str-typed
        # ValidationError.
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "password-require-upper: 2\n"
            "password-require-lower: 1\n"
            "password-require-digit: 1\n"
            "password-require-special: 0\n"
        )
        s = make_settings({}, config_file=str(cfg))
        assert s.password_require_upper == 2
        assert s.password_require_lower == 1
        assert s.password_require_digit == 1
        assert s.password_require_special == 0

    def test_yaml_quoted_string_also_accepted(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text('password-require-upper: "3"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.password_require_upper == 3

    @pytest.mark.parametrize("value", ["-1", "abc", "1.5"])
    def test_malformed_env_rejected(self, value):
        with pytest.raises(Exception, match="password_require_upper"):
            make_settings({"KLANGKD_PASSWORD_REQUIRE_UPPER": value})

    @pytest.mark.parametrize("value", [-1, 0.5, True])
    def test_malformed_yaml_rejected(self, value, tmp_path):
        # Negative, native float, and bool (true -> 1 is a typo, not a
        # window) all abort startup.
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"password-require-upper: {value!s}\n")
        with pytest.raises(Exception, match="password_require_upper"):
            make_settings({}, config_file=str(cfg))

    def test_count_above_password_byte_cap_rejected(self):
        # 73 of one class can never be satisfied (passwords are capped at
        # 72 bytes); startup aborts instead of making every password
        # unsettable.
        with pytest.raises(Exception, match="72"):
            make_settings({"KLANGKD_PASSWORD_REQUIRE_SPECIAL": "73"})
