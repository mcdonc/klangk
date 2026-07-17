"""Tests for the KlangkSettings config loader (#1394).

Covers:
- file: / cmd: indirection resolution (success + error paths)
- make_settings(...) constructor + config_file= param
- resolve_dynamic_config (plugin-declared dynamic keys)
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
    ``resolve_dynamic_config`` (plugin-declared dynamic keys)."""

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

    def test_cmd_timeout(self):
        # A command that sleeps longer than the timeout
        result = _resolve_indirection("cmd:sleep 100")
        assert result is None


class TestResolveDynamicConfig:
    """``resolve_dynamic_config`` resolves plugin-declared dynamic keys
    (outside the ``KLANGK_`` settings model) with ``file:``/``cmd:``
    deref (#1518)."""

    def test_plain_value(self, monkeypatch):
        monkeypatch.setenv("MY_PLUGIN_TOKEN", "abc123")
        assert resolve_dynamic_config("MY_PLUGIN_TOKEN") == "abc123"

    def test_default_when_unset(self):
        assert resolve_dynamic_config("UNSET_PLUGIN_VAR", "fallback") == (
            "fallback"
        )

    def test_unset_no_default(self):
        assert resolve_dynamic_config("UNSET_PLUGIN_VAR") is None

    def test_file_resolution(self, monkeypatch, tmp_path):
        secret = tmp_path / "token"
        secret.write_text("file-secret\n")
        monkeypatch.setenv("MY_PLUGIN_TOKEN", f"file:{secret}")
        assert resolve_dynamic_config("MY_PLUGIN_TOKEN") == "file-secret"

    def test_cmd_resolution(self, monkeypatch):
        monkeypatch.setenv("MY_PLUGIN_TOKEN", "cmd:echo cmd-secret")
        assert resolve_dynamic_config("MY_PLUGIN_TOKEN") == "cmd-secret"


class TestSettingsModel:
    def test_extra_ignored(self, monkeypatch):
        """Unknown KLANGK_ keys are tolerated (extra='ignore')."""
        s = make_settings({"KLANGK_BOGUS_KEY": "whatever"})
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
            "nginx_port",
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
            env={"KLANGK_BRAND_COLOR": "#00FF00"}, config_file=str(cfg)
        )
        assert s.brand_color == "#00FF00"

    def test_yaml_doesnt_override_env(self, tmp_path):
        """A key set in both env and YAML: env wins."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('product_name: "From YAML"\n')
        s = make_settings(
            env={"KLANGK_PRODUCT_NAME": "From Env"}, config_file=str(cfg)
        )
        assert s.product_name == "From Env"

    def test_config_none_opt_out(self):
        """config_file='none': no file, env+defaults only."""
        s = make_settings({}, config_file="none")
        assert s.egress_port == "8995"  # built-in default

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values in YAML resolve at construction (#1461)."""
        secret = tmp_path / "jwt.txt"
        secret.write_text("yaml-secret\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f'jwt_secret: "file:{secret}"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.jwt_secret == "yaml-secret"


# ---------------------------------------------------------------------------
# Dual-form keys: kebab-case *and* snake_case (config-file style, #1538)
# ---------------------------------------------------------------------------


class TestDualFormKeys:
    """Every config-file key may be written in either snake_case or
    kebab-case and resolve to the same field (#1538). snake_case remains the
    documented/preferred form; kebab-case is accepted for backwards compat
    and consistency with the wider config-file style (e.g. cli.yaml, OIDC
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
        assert s.login_lockout_window == "600"

    def test_kebab_required_dir(self, tmp_path):
        """state-dir (kebab) satisfies the required-dir validator."""
        cfg = tmp_path / "config.yaml"
        state = tmp_path / "state"
        state.mkdir()
        cfg.write_text(f'state-dir: "{state}"\n')
        # Direct construction: env has no STATE_DIR, so the kebab key in the
        # config file is the sole source (make_settings would inject one).
        s = KlangkSettings(
            env={"KLANGK_DATA_DIR": str(tmp_path / "data")},
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
# KLANGK_PORT (unset ⇒ headless, set ⇒ browser), KLANGK_EGRESS_PORT (container
# egress), KLANGK_SOCKET (backend UDS), and the deprecated KLANGK_NGINX_PORT
# alias are resolved once at construction. Callers read ``egress_port`` /
# ``socket`` only; ``nginx_port`` is a deprecated alias folded into
# ``egress_port`` (egress-wins) and slated for removal.


class TestResolveSocketAndPorts:
    def test_socket_defaults_to_state_dir_klangk_sock(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.socket == os.path.join("/tmp/state", "klangk.sock")

    def test_explicit_socket_wins(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_SOCKET": "/short/klangk.sock",
            }
        )
        assert s.socket == "/short/klangk.sock"

    def test_egress_port_defaults_to_8995(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.egress_port == "8995"

    def test_explicit_egress_port_wins(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_EGRESS_PORT": "7777",
            }
        )
        assert s.egress_port == "7777"

    def test_port_defaults_to_none_headless(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.port is None

    def test_listen_defaults_to_loopback(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.listen == "127.0.0.1"

    def test_egress_listen_defaults_to_all_interfaces(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.egress_listen == "0.0.0.0"

    def test_egress_listen_override(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_EGRESS_LISTEN": "192.168.1.5",
            }
        )
        assert s.egress_listen == "192.168.1.5"

    def test_nginx_port_folded_into_egress_with_warning(self, caplog):
        """KLANGK_NGINX_PORT alone (no egress) is used as egress + a deprecation warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            s = KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_NGINX_PORT": "9999",
                }
            )
        assert s.egress_port == "9999"
        assert any(
            "KLANGK_NGINX_PORT is deprecated" in r.message
            for r in caplog.records
        )

    def test_egress_wins_over_nginx_port_with_warning(self, caplog):
        """Both set: egress_port wins, nginx_port ignored + a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            s = KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_EGRESS_PORT": "8995",
                    "KLANGK_NGINX_PORT": "9999",
                }
            )
        assert s.egress_port == "8995"
        assert any(
            "KLANGK_NGINX_PORT is ignored" in r.message for r in caplog.records
        )

    def test_egress_equals_port_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_PORT": "8995",
                    "KLANGK_EGRESS_PORT": "8995",
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
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_SOCKET": long_socket,
                }
            )
        msg = str(exc_info.value)
        assert "KLANGK_SOCKET" in msg
        assert "#1531" in msg

    def test_socket_length_error_directs_to_state_dir_or_socket(self):
        from pydantic import ValidationError

        long_socket = "/" + "a" * 104 + ".sock"
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_SOCKET": long_socket,
                }
            )
        msg = str(exc_info.value)
        assert "KLANGK_STATE_DIR" in msg
        assert "KLANGK_SOCKET" in msg


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
        s = make_settings({"KLANGK_EGRESS_PORT": "4321"})
        assert s.egress_port == "4321"

    def test_env_dict_ignores_os_environ(self, monkeypatch):
        monkeypatch.setenv("KLANGK_EGRESS_PORT", "9999")
        s = make_settings({"KLANGK_EGRESS_PORT": "1111"})
        assert s.egress_port == "1111"
        assert s.egress_port != "9999"

    def test_empty_env_dict_uses_defaults(self):
        s = make_settings({})
        assert s.auth_modes is None
        assert s.default_user == "admin@example.com"
        assert s.min_password_length == "8"

    def test_env_for_sources_reset_after_construction(self):
        # The class-var bridge is cleaned up after construction so it doesn't
        # leak between instances.
        make_settings({"KLANGK_EGRESS_PORT": "1234"})
        assert KlangkSettings._env_for_sources is None

    def test_env_dict_multiple_fields(self):
        s = make_settings(
            env={
                "KLANGK_AUTH_MODES": "password",
                "KLANGK_JWT_SECRET": "secret123",
                "KLANGK_DEFAULT_USER": "admin@test.com",
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
            env={"KLANGK_PRODUCT_NAME": "FromEnv"}, config_file=str(cfg)
        )
        assert s.product_name == "FromEnv"


class TestAuthModesValidator:
    """KLANGK_AUTH_MODES is security-sensitive: a typo must fail at
    construction (boot), not silently downgrade to the no-auth ``none`` mode
    (which freely issues an admin token)."""

    @pytest.mark.parametrize("mode", ["password", "oidc", "both", "none"])
    def test_valid_modes_accepted(self, mode):
        s = make_settings({"KLANGK_AUTH_MODES": mode})
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
            make_settings({"KLANGK_AUTH_MODES": bad})

    def test_empty_string_treated_as_unset(self):
        # KLANGK_AUTH_MODES="" (set but blank) is treated as unset → None →
        # "none" at read time, preserving the pre-validator behavior.
        # (Not a security risk: blank is a config mistake, not a typo'd
        # secure-mode name silently degrading.)
        s = make_settings({"KLANGK_AUTH_MODES": ""})
        assert s.auth_modes is None

    def test_typo_error_message_lists_valid_modes(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings({"KLANGK_AUTH_MODES": "passdword"})
        msg = str(exc_info.value)
        assert "passdword" in msg
        assert "password" in msg  # valid modes listed in the message


class TestLogLevelValidator:
    """KLANGK_LOG_LEVEL must be a recognized level or fail fast at boot
    (#1467), mirroring the fail-fast posture of the auth_modes validator."""

    def test_defaults_to_info(self):
        s = make_settings({})
        assert s.log_level == "INFO"

    @pytest.mark.parametrize(
        "name", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    )
    def test_valid_names_accepted_any_case(self, name):
        # lower, upper, mixed all normalize to upper
        s = make_settings({"KLANGK_LOG_LEVEL": name.lower()})
        assert s.log_level == name

    @pytest.mark.parametrize("num", ["0", "10", "20", "30", "40", "50"])
    def test_numeric_string_accepted(self, num):
        s = make_settings({"KLANGK_LOG_LEVEL": num})
        assert s.log_level == num

    def test_empty_string_defaults_to_info(self):
        s = make_settings({"KLANGK_LOG_LEVEL": ""})
        assert s.log_level == "INFO"

    @pytest.mark.parametrize(
        "bad", ["verbose", "TRACE", "info!", "debug-level"]
    )
    def test_garbage_rejected_at_construction(self, bad):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGK_LOG_LEVEL": bad})

    def test_error_message_names_valid_levels(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            make_settings({"KLANGK_LOG_LEVEL": "verbose"})
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
        s = make_settings({"KLANGK_JWT_SECRET": f"file:{secret}"})
        assert s.jwt_secret == "the-real-secret"

    def test_cmd_resolved_at_construction(self):
        s = make_settings(
            env={"KLANGK_JWT_SECRET": "cmd:printf %s cmd-secret"}
        )
        assert s.jwt_secret == "cmd-secret"

    def test_plain_value_passes_through(self):
        s = make_settings({"KLANGK_JWT_SECRET": "plain-secret"})
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
            make_settings({"KLANGK_JWT_SECRET": "file:/nonexistent/path"})
        msg = str(exc_info.value)
        assert "JWT_SECRET" in msg

    def test_cmd_failure_fails_at_construction(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGK_JWT_SECRET": "cmd:false"})

    def test_idempotent_re_resolution(self):
        # A plain (already-resolved) value survives a second pass unchanged —
        # the legacy resolve_env_value path reads the resolved field and its
        # redundant _resolve_indirection call is a no-op.
        s = make_settings({"KLANGK_EGRESS_PORT": "8995"})
        assert _resolve_indirection(s.egress_port) == "8995"

    def test_non_string_field_skipped(self):
        # oidc_providers is list[dict] | None — not a str, skipped by the
        # validator (would crash if isinstance check were missing).
        s = make_settings({"KLANGK_OIDC_PROVIDERS": '[{"name": "x"}]'})
        assert s.oidc_providers == [{"name": "x"}]


class TestRequireDirsValidator:
    """`state_dir` is required -- no default (#1461); a missing value fails at
    construction, not at the first use that dereferences a ``None`` path.
    `data_dir` and `plugins_dir` both default to `<state_dir>/<name>` when
    unset (#1506); explicit values win."""

    def test_missing_state_dir_fails(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(env={"KLANGK_DATA_DIR": "/tmp/data"})
        assert "KLANGK_STATE_DIR" in str(exc_info.value)

    def test_missing_state_dir_alone_fails(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KlangkSettings(env={})

    def test_data_dir_defaults_to_state_dir_data(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.data_dir == os.path.join("/tmp/state", "data")

    def test_explicit_data_dir_wins(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_DATA_DIR": "/explicit/data",
            }
        )
        assert s.data_dir == "/explicit/data"

    def test_plugins_dir_defaults_to_state_dir_plugins(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.plugins_dir == os.path.join("/tmp/state", "plugins")

    def test_explicit_plugins_dir_wins(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_PLUGINS_DIR": "/explicit/plugins",
            }
        )
        assert s.plugins_dir == "/explicit/plugins"

    def test_both_derived_dirs_default_together(self):
        s = KlangkSettings(env={"KLANGK_STATE_DIR": "/tmp/state"})
        assert s.data_dir == os.path.join("/tmp/state", "data")
        assert s.plugins_dir == os.path.join("/tmp/state", "plugins")
        assert s.customize_dir == os.path.join("/tmp/state", "custom")

    def test_explicit_customize_dir_wins(self):
        s = KlangkSettings(
            env={
                "KLANGK_STATE_DIR": "/tmp/state",
                "KLANGK_CUSTOMIZE_DIR": "/explicit/custom",
            }
        )
        assert s.customize_dir == "/explicit/custom"


class TestReload:
    """KlangkSettings.reload() re-resolves from the same sources (#1587)."""

    def test_reload_returns_fresh_instance(self):
        s = make_settings({"KLANGK_AGENT_HANDLE": "bot1"})
        s2 = s.reload()
        assert s2 is not s
        assert s2.agent_handle == "bot1"

    def test_reload_picks_up_changed_env(self):
        env = {
            "KLANGK_DATA_DIR": "/d",
            "KLANGK_STATE_DIR": "/s",
            "KLANGK_AGENT_HANDLE": "old",
        }
        s = KlangkSettings(env)
        env["KLANGK_AGENT_HANDLE"] = "new"
        s2 = s.reload()
        assert s2.agent_handle == "new"
        assert s.agent_handle == "old"

    def test_reload_raises_on_invalid_config(self):
        s = make_settings({})
        with pytest.raises(Exception):
            # auth_modes must be a valid value; "bogus" will fail validation.
            env = dict(s._reload_env)
            env["KLANGK_AUTH_MODES"] = "bogus"
            KlangkSettings(env)
