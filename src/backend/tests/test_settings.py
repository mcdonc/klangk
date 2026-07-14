"""Tests for the KlangkSettings config loader (#1394).

Covers:
- file: / cmd: indirection resolution (success + error paths)
- make_settings(...) constructor + config_file= param
- resolve_dynamic_config (plugin-declared dynamic keys)
"""

import os

import pytest

from _helpers import make_settings
from klangk_backend.settings import (
    KlangkSettings,
    _resolve_indirection,
    classify_listen,
    listen_is_socket,
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
        assert s.nginx_port == "8995"  # built-in default

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
    handled separately by :func:`klangk_backend.oidc.get`."""

    def test_kebab_case_key_loads(self, tmp_path):
        """A hyphenated top-level key maps to its snake_case field."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('nginx-port: "9999"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.nginx_port == "9999"

    def test_snake_case_key_loads(self, tmp_path):
        """snake_case (the documented form) still loads unchanged."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('nginx_port: "7777"\n')
        s = make_settings({}, config_file=str(cfg))
        assert s.nginx_port == "7777"

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
# classify_listen / listen_is_socket (polymorphic KLANGK_LISTEN, #1422)
# ---------------------------------------------------------------------------
# KLANGK_LISTEN is polymorphic: a socket path or a TCP host (no port —
# the port comes from KLANGK_PORT). The
# deployment shape is *derived* from listen's shape + auth_modes (there is
# no amalgamated UI-mode/preset setting — it never shipped). These pin the
# classifier that the renderer, the klangkd bind, and the CLI (#1399) share.


class TestClassifyListen:
    def test_absolute_path_is_socket(self):
        assert classify_listen("/tmp/klangk.sock") == "socket"
        assert classify_listen("/run/user/1000/klangk.sock") == "socket"

    def test_tcp_host_is_tcp(self):
        assert classify_listen("127.0.0.1") == "tcp"
        assert classify_listen("0.0.0.0") == "tcp"
        assert classify_listen("::1") == "tcp"

    def test_value_with_port_is_tcp(self):
        # LISTEN never carries a port (it comes from KLANGK_PORT), but if a
        # port-bearing value did appear it must classify as TCP, not socket —
        # the classifier never mis-classifies a non-socket.
        assert classify_listen("127.0.0.1:8997") == "tcp"
        assert classify_listen("0.0.0.0:8997") == "tcp"
        assert classify_listen("[::1]:8997") == "tcp"

    def test_http_scheme_is_tcp(self):
        # CLI-style absolute URL — TCP (shared convention with #1399).
        assert classify_listen("http://host:8995") == "tcp"
        assert classify_listen("https://host") == "tcp"

    def test_none_is_tcp(self):
        # None ⇒ the TCP default (loopback until #1400 flips it to a socket).
        assert classify_listen(None) == "tcp"
        assert classify_listen("") == "tcp"

    def test_bare_relative_is_tcp(self):
        # Ambiguous (no scheme, no leading slash) — classified as TCP, not a
        # socket. Callers needing a socket must pass an absolute path; this
        # keeps the classifier total (mirrors #1399's absolute-path rule).
        assert classify_listen("klangk.sock") == "tcp"
        assert classify_listen("localhost") == "tcp"


class TestListenIsSocket:
    def test_true_when_listen_is_socket_path(self):
        assert listen_is_socket("/tmp/klangk.sock") is True

    def test_false_when_listen_is_tcp(self):
        assert listen_is_socket("127.0.0.1") is False

    def test_false_for_default(self):
        # The default (127.0.0.1) is TCP; #1400 will flip this to a socket.
        assert listen_is_socket("127.0.0.1") is False


class TestKlangkdLauncher:
    """Tests for the klangkd launcher's --config resolution."""

    def test_resolve_config_path_existing(self, tmp_path):
        from klangk_backend.klangkd import _resolve_config_path

        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: test\n")
        assert _resolve_config_path(str(cfg)) == str(cfg)

    def test_resolve_config_path_none(self):
        from klangk_backend.klangkd import _resolve_config_path

        assert _resolve_config_path("none") == "none"

    def test_resolve_config_path_missing(self):
        import pytest as _pytest
        from klangk_backend.klangkd import _resolve_config_path
        import typer

        with _pytest.raises(typer.BadParameter):
            _resolve_config_path("/nonexistent/path/to/config.yaml")


class TestEnvConstructor:
    """Tests for the make_settings(...) constructor (#1426 Slice 1)."""

    def test_reads_from_env_dict(self):
        # Explicit env dict is the only source — os.environ is ignored.
        s = make_settings({"KLANGK_NGINX_PORT": "4321"})
        assert s.nginx_port == "4321"

    def test_env_dict_ignores_os_environ(self, monkeypatch):
        monkeypatch.setenv("KLANGK_NGINX_PORT", "9999")
        s = make_settings({"KLANGK_NGINX_PORT": "1111"})
        assert s.nginx_port == "1111"
        assert s.nginx_port != "9999"

    def test_empty_env_dict_uses_defaults(self):
        s = make_settings({})
        assert s.auth_modes is None
        assert s.default_user == "admin@example.com"
        assert s.min_password_length == "8"

    def test_env_for_sources_reset_after_construction(self):
        # The class-var bridge is cleaned up after construction so it doesn't
        # leak between instances.
        make_settings({"KLANGK_NGINX_PORT": "1234"})
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
        s = make_settings({"KLANGK_NGINX_PORT": "8995"})
        assert _resolve_indirection(s.nginx_port) == "8995"

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
