"""Tests for the KlangkSettings config loader (#1394).

Covers:
- file: / cmd: indirection resolution (success + error paths)
- get_settings env-change-detection cache
- resolve_env_value (KLANGK_ and non-KLANGK_ keys)
- resolve_env_bool
- validate_at_startup
- _key_to_field mapping
"""

import pytest

from klangk_backend import settings as settings_mod
from klangk_backend.exceptions import ConfigurationError
from klangk_backend.settings import (
    KlangkSettings,
    PRESETS,
    _key_to_field,
    get_config_file,
    get_settings,
    resolve_env_bool,
    resolve_env_value,
    resolve_indirection,
    set_config_file,
    validate_at_startup,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the settings cache before and after each test."""
    settings_mod._invalidate_cache()
    yield
    settings_mod._invalidate_cache()


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
        monkeypatch.setenv("KLANGK_NGINX_PORT", "1111")
        assert get_settings().nginx_port == "1111"
        monkeypatch.setenv("KLANGK_NGINX_PORT", "2222")
        assert get_settings().nginx_port == "2222"

    def test_cache_holds_when_env_stable(self, monkeypatch):
        monkeypatch.setenv("KLANGK_NGINX_PORT", "3333")
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

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

    def test_primes_cache(self):
        s = validate_at_startup()
        assert get_settings() is s

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
# KLANGK_PRESET (#1397): the single deployment-shape key
# ---------------------------------------------------------------------------


class TestPreset:
    """``KLANGK_PRESET`` is the only settable deployment-shape key (#1397).

    The earlier #1397 draft exposed four axis keys (preset / auth /
    browser_ingress / container_egress_paths). The finalized model keeps only
    ``preset``: the auth GATE is its ``-auth``/``-noauth`` suffix, browser
    ingress is implied by the ``ip-*`` presets, and container egress paths
    are a fixed per-preset default. The auth BACKEND (password vs OIDC vs
    both) stays the operator's choice via the existing ``KLANGK_AUTH_MODES``;
    the two are cross-validated (see :class:`TestPresetAuthConflict`).

    These tests pin that ``preset`` is settable via BOTH an env var and the
    YAML config file (like every other field) so a future change can't
    silently drop the config-file path.
    """

    def test_field_exists(self):
        assert "preset" in KlangkSettings.model_fields
        assert PRESETS == frozenset(
            {"uds-noauth", "uds-auth", "ip-noauth", "ip-auth"}
        )

    def test_dropped_axis_keys_are_not_fields(self):
        # auth / browser_ingress / container_egress_paths are NOT individually
        # settable — everything but preset is derived from the preset.
        fields = KlangkSettings.model_fields
        for dropped in ("auth", "browser_ingress", "container_egress_paths"):
            assert dropped not in fields, dropped

    @pytest.mark.parametrize("value", sorted(PRESETS))
    def test_settable_via_env(self, monkeypatch, value):
        set_config_file(None)
        monkeypatch.setenv("KLANGK_PRESET", value)
        assert get_settings().preset == value

    @pytest.mark.parametrize("value", sorted(PRESETS))
    def test_settable_via_yaml(self, tmp_path, value):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f'preset: "{value}"\n')
        set_config_file(str(cfg))
        assert get_settings().preset == value

    def test_env_overrides_yaml(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text('preset: "uds-noauth"\n')
        set_config_file(str(cfg))
        monkeypatch.setenv("KLANGK_PRESET", "ip-auth")
        assert get_settings().preset == "ip-auth"

    def test_default_none_preserves_today(self):
        # No preset set → None → pre-#1392 behavior; validate_at_startup is a
        # no-op (no conflict check runs when preset is unset).
        set_config_file(None)
        assert get_settings().preset is None


class TestPresetAuthConflict:
    """``KLANGK_PRESET`` must agree with an EXPLICIT ``KLANGK_AUTH_MODES``.

    The preset fixes whether an auth gate is required (its suffix); the
    operator separately chooses the backend (password / OIDC / both) via
    ``KLANGK_AUTH_MODES``. The two are cross-validated at config-load by
    :func:`validate_at_startup`, so an *explicitly conflicting* config fails
    fast at boot:

    - ``*-noauth`` presets require the resolved auth mode to be ``none``;
    - ``*-auth``   presets require it to be non-``none``.

    Important: an UNSET ``KLANGK_AUTH_MODES`` never conflicts —
    ``oidc.auth_modes()`` is preset-aware in the unset path (#1397), so a
    ``*-auth`` preset defaults the mode to ``password`` and ``*-noauth`` to
    ``none``. The conflict check only fires on an *explicit* value that
    disagrees with the preset. These tests set ``KLANGK_AUTH_MODES``
    explicitly in every case; the unset-no-conflict cases are covered at the
    end.
    """

    def test_unknown_preset_rejected(self, monkeypatch):
        monkeypatch.setenv("KLANGK_PRESET", "bogus")
        monkeypatch.setenv("KLANGK_AUTH_MODES", "password")
        with pytest.raises(ConfigurationError, match="not one of"):
            validate_at_startup()

    @pytest.mark.parametrize("preset", ["uds-noauth", "ip-noauth"])
    @pytest.mark.parametrize("mode", ["password", "oidc", "both"])
    def test_noauth_preset_conflicts_with_any_backend(
        self, monkeypatch, preset, mode
    ):
        monkeypatch.setenv("KLANGK_PRESET", preset)
        monkeypatch.setenv("KLANGK_AUTH_MODES", mode)
        with pytest.raises(
            ConfigurationError, match="requires KLANGK_AUTH_MODES=none"
        ):
            validate_at_startup()

    @pytest.mark.parametrize("preset", ["uds-noauth", "ip-noauth"])
    def test_noauth_preset_ok_with_none(self, monkeypatch, preset):
        monkeypatch.setenv("KLANGK_PRESET", preset)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        settings = validate_at_startup()  # no raise
        assert settings.preset == preset

    @pytest.mark.parametrize("preset", ["uds-auth", "ip-auth"])
    def test_auth_preset_conflicts_with_none(self, monkeypatch, preset):
        monkeypatch.setenv("KLANGK_PRESET", preset)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        with pytest.raises(
            ConfigurationError, match="requires an auth-gated backend"
        ):
            validate_at_startup()

    @pytest.mark.parametrize("preset", ["uds-auth", "ip-auth"])
    @pytest.mark.parametrize("mode", ["password", "oidc", "both"])
    def test_auth_preset_ok_with_backend(self, monkeypatch, preset, mode):
        monkeypatch.setenv("KLANGK_PRESET", preset)
        monkeypatch.setenv("KLANGK_AUTH_MODES", mode)
        settings = validate_at_startup()  # no raise
        assert settings.preset == preset

    def test_no_preset_skips_conflict_check(self, monkeypatch):
        # preset unset → no validation runs (pre-#1392 behavior preserved),
        # regardless of the auth mode.
        monkeypatch.delenv("KLANGK_PRESET", raising=False)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        validate_at_startup()  # no raise

    @pytest.mark.parametrize(
        "preset", ["uds-noauth", "uds-auth", "ip-noauth", "ip-auth"]
    )
    def test_unset_auth_mode_never_conflicts(self, monkeypatch, preset):
        # An unset KLANGK_AUTH_MODES never conflicts: oidc.auth_modes()
        # self-defaults to match the preset (*-auth → password, *-noauth →
        # none). So a preset alone boots cleanly with no explicit backend.
        monkeypatch.setenv("KLANGK_PRESET", preset)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        settings = validate_at_startup()  # no raise
        assert settings.preset == preset


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
