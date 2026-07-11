"""Tests for the KlangkSettings config loader (#1394).

Covers:
- file: / cmd: indirection resolution (success + error paths)
- get_settings env-change-detection cache
- resolve_env_value (KLANGK_ and non-KLANGK_ keys)
- resolve_env_bool
- validate_at_startup
- _key_to_field mapping
"""

import os

import pytest

from klangk_backend import settings as settings_mod
from klangk_backend.settings import (
    KlangkSettings,
    _key_to_field,
    get_config_file,
    get_settings,
    resolve_env_bool,
    resolve_env_value,
    classify_listen,
    listen_is_socket,
    resolve_indirection,
    set_config_file,
    validate_at_startup,
)


class TestKeyToField:
    def test_klangk_prefix(self):
        assert _key_to_field("KLANGK_JWT_SECRET") == "jwt_secret"

    def test_multi_word(self):
        assert (
            _key_to_field("KLANGK_ACCESS_TOKEN_HOURS") == "access_token_hours"
        )

    def test_non_klangk(self):
        assert _key_to_field("LOGFIRE_TOKEN") == "logfire_token"


class TestResolveIndirection:
    def test_none_returns_none(self):
        assert resolve_indirection(None) is None

    def test_plain_value(self):
        assert resolve_indirection("hello") == "hello"

    def test_file_prefix(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("the-secret\n")
        assert resolve_indirection(f"file:{secret}") == "the-secret"

    def test_file_failure_returns_none(self):
        result = resolve_indirection("file:/nonexistent/path/to/secret")
        assert result is None

    def test_cmd_prefix(self):
        result = resolve_indirection("cmd:echo hello")
        assert result == "hello"

    def test_cmd_failure_returns_none(self):
        result = resolve_indirection("cmd:false")
        assert result is None

    def test_cmd_nonzero_exit_returns_none(self):
        result = resolve_indirection("cmd:exit 1")
        assert result is None

    def test_cmd_oserror(self):
        # A command that can't be spawned (no such binary)
        result = resolve_indirection("cmd:/nonexistent/binary/path")
        assert result is None

    def test_cmd_timeout(self):
        # A command that sleeps longer than the timeout
        result = resolve_indirection("cmd:sleep 100")
        assert result is None


class TestGetSettings:
    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("KLANGK_NGINX_PORT", "12345")
        s = get_settings()
        assert s.nginx_port == "12345"

    def test_cache_invalidated_on_env_change(self, monkeypatch):
        # get_settings() is cache-free — constructs on every call. Env
        # changes are automatically picked up (no cache to invalidate).
        monkeypatch.setenv("KLANGK_NGINX_PORT", "1111")
        assert get_settings().nginx_port == "1111"
        monkeypatch.setenv("KLANGK_NGINX_PORT", "2222")
        assert get_settings().nginx_port == "2222"

    def test_cache_free_fresh_each_call(self, monkeypatch):
        # get_settings() constructs a new instance every call (cache machinery
        # deleted in #1426 Slice 1). Two calls return equivalent but distinct
        # objects.
        monkeypatch.setenv("KLANGK_NGINX_PORT", "3333")
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is not s2
        assert s1 == s2

    def test_delenv_invalidates(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        assert get_settings().auth_modes == "none"
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        assert get_settings().auth_modes is None

    def test_defaults(self):
        s = get_settings()
        assert s.product_name == "Klangk"
        assert s.podman_bin == "podman"


class TestResolveEnvValue:
    def test_klangk_key(self, monkeypatch):
        monkeypatch.setenv("KLANGK_JWT_SECRET", "secret123")
        assert resolve_env_value("KLANGK_JWT_SECRET") == "secret123"

    def test_klangk_key_default(self):
        assert (
            resolve_env_value("KLANGK_NONEXISTENT", "fallback") == "fallback"
        )

    def test_klangk_key_unset_no_default(self):
        assert resolve_env_value("KLANGK_NONEXISTENT") is None

    def test_non_klangk_key(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "lf-token")
        assert resolve_env_value("LOGFIRE_TOKEN") == "lf-token"

    def test_non_klangk_key_default(self):
        assert resolve_env_value("SOME_OTHER_VAR", "def") == "def"

    def test_file_resolution(self, monkeypatch, tmp_path):
        secret = tmp_path / "jwt"
        secret.write_text("file-secret\n")
        monkeypatch.setenv("KLANGK_JWT_SECRET", f"file:{secret}")
        assert resolve_env_value("KLANGK_JWT_SECRET") == "file-secret"

    def test_cmd_resolution(self, monkeypatch):
        monkeypatch.setenv("KLANGK_JWT_SECRET", "cmd:echo cmd-secret")
        assert resolve_env_value("KLANGK_JWT_SECRET") == "cmd-secret"


class TestResolveEnvBool:
    def test_truthy_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "Yes"):
            monkeypatch.setenv("KLANGK_TEST_MODE", val)
            assert resolve_env_bool("KLANGK_TEST_MODE") is True, val

    def test_falsy_values(self, monkeypatch):
        for val in ("0", "false", "no", "", "banana"):
            monkeypatch.setenv("KLANGK_TEST_MODE", val)
            assert resolve_env_bool("KLANGK_TEST_MODE") is False, val

    def test_unset_default(self):
        assert resolve_env_bool("KLANGK_NONEXISTENT") is False
        assert resolve_env_bool("KLANGK_NONEXISTENT", True) is True


class TestValidateAtStartup:
    def test_returns_settings(self):
        s = validate_at_startup()
        assert isinstance(s, KlangkSettings)

    def test_reads_env(self, monkeypatch):
        # validate_at_startup() is cache-free now (no cache to prime); it just
        # constructs settings from the live env.
        monkeypatch.setenv("KLANGK_NGINX_PORT", "5555")
        s = validate_at_startup()
        assert s.nginx_port == "5555"

    def test_re_validates_after_env_change(self, monkeypatch):
        monkeypatch.setenv("KLANGK_NGINX_PORT", "5555")
        validate_at_startup()
        assert get_settings().nginx_port == "5555"


class TestSettingsModel:
    def test_extra_ignored(self, monkeypatch):
        """Unknown KLANGK_ keys are tolerated (extra='ignore')."""
        monkeypatch.setenv("KLANGK_BOGUS_KEY", "whatever")
        # Should not raise
        s = get_settings()
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
        set_config_file(str(cfg))
        s = get_settings()
        assert s.logo_url == "https://example.com/logo.png"

    def test_env_overrides_yaml(self, monkeypatch, tmp_path):
        """Env vars override YAML file values (precedence)."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('brand_color: "#FF0000"\n')
        set_config_file(str(cfg))
        monkeypatch.setenv("KLANGK_BRAND_COLOR", "#00FF00")
        s = get_settings()
        assert s.brand_color == "#00FF00"

    def test_yaml_doesnt_override_env(self, monkeypatch, tmp_path):
        """A key set in both env and YAML: env wins."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('product_name: "From YAML"\n')
        set_config_file(str(cfg))
        monkeypatch.setenv("KLANGK_PRODUCT_NAME", "From Env")
        s = get_settings()
        assert s.product_name == "From Env"

    def test_config_none_opt_out(self, monkeypatch):
        """--config=none: no file, env+defaults only."""
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        set_config_file("none")
        s = get_settings()
        assert s.nginx_port == "8995"  # built-in default

    def test_set_config_file_invalidates_cache(self, tmp_path):
        """Changing the config-file path re-instantiates settings."""
        cfg1 = tmp_path / "c1.yaml"
        cfg1.write_text('product_name: "First"\n')
        set_config_file(str(cfg1))
        assert get_settings().product_name == "First"
        cfg2 = tmp_path / "c2.yaml"
        cfg2.write_text('product_name: "Second"\n')
        set_config_file(str(cfg2))
        assert get_settings().product_name == "Second"

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values in YAML resolve correctly."""
        secret = tmp_path / "jwt.txt"
        secret.write_text("yaml-secret\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f'jwt_secret: "file:{secret}"\n')
        set_config_file(str(cfg))
        # resolve_env_value applies file:/cmd: resolution
        assert resolve_env_value("KLANGK_JWT_SECRET") == "yaml-secret"

    def test_get_config_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: test\n")
        set_config_file(str(cfg))
        assert get_config_file() == str(cfg)
        set_config_file(None)
        assert get_config_file() is None


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
    def test_true_when_listen_is_socket_path(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LISTEN", "/tmp/klangk.sock")
        assert listen_is_socket() is True

    def test_false_when_listen_is_tcp(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LISTEN", "127.0.0.1")
        assert listen_is_socket() is False

    def test_false_for_default(self, monkeypatch):
        # The default (127.0.0.1) is TCP; #1400 will flip this to a socket.
        monkeypatch.delenv("KLANGK_LISTEN", raising=False)
        assert listen_is_socket() is False


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
    """Tests for the KlangkSettings(env=...) constructor (#1426 Slice 1)."""

    def test_reads_from_env_dict(self):
        # Explicit env dict is the only source — os.environ is ignored.
        s = KlangkSettings(env={"KLANGK_NGINX_PORT": "4321"})
        assert s.nginx_port == "4321"

    def test_env_dict_ignores_os_environ(self, monkeypatch):
        monkeypatch.setenv("KLANGK_NGINX_PORT", "9999")
        s = KlangkSettings(env={"KLANGK_NGINX_PORT": "1111"})
        assert s.nginx_port == "1111"
        assert s.nginx_port != "9999"

    def test_empty_env_dict_uses_defaults(self):
        s = KlangkSettings(env={})
        assert s.auth_modes is None
        assert s.default_user == "admin@example.com"
        assert s.min_password_length == "8"

    def test_default_reads_os_environ(self, monkeypatch):
        # KlangkSettings(os.environ) reads from os.environ; monkeypatch.setenv
        # mutates os.environ, so the constructed settings see the value.
        monkeypatch.setenv("KLANGK_AUTH_MODES", "oidc")
        s = KlangkSettings(os.environ)
        assert s.auth_modes == "oidc"

    def test_env_for_sources_reset_after_construction(self):
        # The class-var bridge is cleaned up after construction so it doesn't
        # leak between instances.
        KlangkSettings(env={"KLANGK_NGINX_PORT": "1234"})
        assert KlangkSettings._env_for_sources is None

    def test_env_dict_multiple_fields(self):
        s = KlangkSettings(
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
        # The config_file= constructor param wires a YAML source in, with no
        # help from the module-global set_config_file().
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: FromConfigFile\n")
        s = KlangkSettings(env={}, config_file=str(cfg))
        assert s.product_name == "FromConfigFile"

    def test_config_file_param_beats_module_global(self, tmp_path, monkeypatch):
        # When both the constructor param and the module global are set, the
        # constructor param wins (it's the intended path; the global is the
        # legacy fallback slated for deletion).
        from klangk_backend.settings import set_config_file

        global_cfg = tmp_path / "global.yaml"
        global_cfg.write_text("product_name: FromGlobal\n")
        param_cfg = tmp_path / "param.yaml"
        param_cfg.write_text("product_name: FromParam\n")
        set_config_file(str(global_cfg))
        try:
            s = KlangkSettings(env={}, config_file=str(param_cfg))
            assert s.product_name == "FromParam"
        finally:
            set_config_file(None)

    def test_env_overrides_config_file(self, tmp_path):
        # Precedence: env dict > config file.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: FromConfigFile\n")
        s = KlangkSettings(
            env={"KLANGK_PRODUCT_NAME": "FromEnv"}, config_file=str(cfg)
        )
        assert s.product_name == "FromEnv"


class TestAuthModesValidator:
    """KLANGK_AUTH_MODES is security-sensitive: a typo must fail at
    construction (boot), not silently downgrade to the no-auth ``none`` mode
    (which freely issues an admin token)."""

    @pytest.mark.parametrize("mode", ["password", "oidc", "both", "none"])
    def test_valid_modes_accepted(self, mode):
        s = KlangkSettings(env={"KLANGK_AUTH_MODES": mode})
        assert s.auth_modes == mode

    def test_unset_allowed_means_none(self):
        # None = unset = "default to none at read time" (legitimate).
        s = KlangkSettings(env={})
        assert s.auth_modes is None

    @pytest.mark.parametrize(
        "bad", ["passdword", "PASSWORD", " true", "x", "None"]
    )
    def test_typo_rejected_at_construction(self, bad):
        # A set-but-garbage value must raise (not silently become "none").
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            KlangkSettings(env={"KLANGK_AUTH_MODES": bad})

    def test_empty_string_treated_as_unset(self):
        # KLANGK_AUTH_MODES="" (set but blank) is treated as unset → None →
        # "none" at read time, preserving the pre-validator behavior.
        # (Not a security risk: blank is a config mistake, not a typo'd
        # secure-mode name silently degrading.)
        s = KlangkSettings(env={"KLANGK_AUTH_MODES": ""})
        assert s.auth_modes is None

    def test_typo_error_message_lists_valid_modes(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(env={"KLANGK_AUTH_MODES": "passdword"})
        msg = str(exc_info.value)
        assert "passdword" in msg
        assert "password" in msg  # valid modes listed in the message
