"""Tests for plugins module (feature-manifest model, #1655).

The runtime no longer scans ``KLANGK_PLUGINS_DIR`` for per-plugin
``package.json`` files — that presumed materialized source trees on the
klangkd host, which pip/uv installs never have. Instead the build emits one
``features.json`` into the frontend bundle dir, and ``Plugins`` reads it at
construction. These tests cover the new model: manifest parsing, the
container-env key bridge, frontend-scope config values, the feature list for
``/api/version``, and the ``features_enable`` knob forwarding.
"""

import json
import types

from klangk import plugins
from _helpers import make_settings


def _write_manifest(frontend_dir, manifest):
    """Write features.json at <frontend_dir>/features.json (#1655)."""
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "features.json").write_text(json.dumps(manifest))


def _plugins(frontend_dir, env=None):
    """Build a fresh Plugins instance whose frontend_dir is *frontend_dir*."""
    settings_env = {"KLANGK_FRONTEND_DIR": str(frontend_dir)}
    if env:
        settings_env.update(env)
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings(settings_env))
    )
    return plugins.Plugins(app_state)


class TestFeatureList:
    """feature_list() backs /api/version's `plugins` field — the full set of
    features possible to use on this install (regardless of activation)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        # No features.json at the frontend dir → empty feature list.
        p = _plugins(tmp_path)
        assert p.feature_list() == []

    def test_missing_manifest_file(self, tmp_path):
        p = _plugins(tmp_path / "nonexistent")
        assert p.feature_list() == []

    def test_returns_metadata(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "celebrate",
                        "version": "1.0.0",
                        "description": "A feature",
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        result = p.feature_list()
        assert result == [
            {
                "name": "celebrate",
                "version": "1.0.0",
                "description": "A feature",
            }
        ]

    def test_missing_fields_default_empty(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [{"name": "minimal"}],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        assert p.feature_list() == [
            {"name": "minimal", "version": "", "description": ""}
        ]

    def test_invalid_json_returns_empty(self, tmp_path):
        (tmp_path / "features.json").write_text("not json")
        p = _plugins(tmp_path)
        assert p.feature_list() == []

    def test_non_dict_manifest_returns_empty(self, tmp_path):
        (tmp_path / "features.json").write_text('["not", "a", "dict"]')
        p = _plugins(tmp_path)
        assert p.feature_list() == []

    def test_non_dict_feature_entry_skipped(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": ["not-a-dict", {"name": "ok"}],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        assert p.feature_list() == [
            {"name": "ok", "version": "", "description": ""}
        ]


class TestContainerEnv:
    """container_env() reads the build-emitted container_env_keys list and
    resolves each from the server env (the bridge into workspace containers)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        p = _plugins(tmp_path)
        assert p.container_env() == {}

    def test_no_keys_returns_empty(self, tmp_path):
        _write_manifest(
            tmp_path,
            {"features": [], "defaults": [], "container_env_keys": []},
        )
        p = _plugins(tmp_path)
        assert p.container_env() == {}

    def test_resolves_keys_from_env(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_GITHUB_OAUTH_CLIENT_ID"],
            },
        )
        monkeypatch.setenv("KLANGK_GITHUB_OAUTH_CLIENT_ID", "abc123")
        p = _plugins(tmp_path)
        assert p.container_env() == {"KLANGK_GITHUB_OAUTH_CLIENT_ID": "abc123"}

    def test_unset_key_resolves_empty(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["UNSET_KEY"],
            },
        )
        monkeypatch.delenv("UNSET_KEY", raising=False)
        p = _plugins(tmp_path)
        # No default carried in the key-list (only the names); unresolved → "".
        assert p.container_env() == {"UNSET_KEY": ""}

    def test_multiple_keys(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["A_KEY", "B_KEY"],
            },
        )
        monkeypatch.setenv("A_KEY", "a-val")
        monkeypatch.setenv("B_KEY", "b-val")
        p = _plugins(tmp_path)
        assert p.container_env() == {"A_KEY": "a-val", "B_KEY": "b-val"}

    def test_non_string_key_skipped(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["OK_KEY", 42, None],
            },
        )
        monkeypatch.setenv("OK_KEY", "ok")
        p = _plugins(tmp_path)
        assert p.container_env() == {"OK_KEY": "ok"}


class TestFrontendConfig:
    """frontend_config() resolves frontend/both-scope values from the
    per-feature config blocks (shape from the manifest, values from the env)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        p = _plugins(tmp_path)
        assert p.frontend_config() == {}

    def test_frontend_scope_value_resolved_lowercased(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "soliplex",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "SOLIPLEX_URL": {
                                "description": "RAG endpoint",
                                "default": "",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv("SOLIPLEX_URL", "https://rag.example.com")
        p = _plugins(tmp_path)
        assert p.frontend_config() == {
            "soliplex_url": "https://rag.example.com"
        }

    def test_both_scope_appears_in_frontend_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "shared",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "SHARED_URL": {
                                "description": "",
                                "default": "http://default",
                                "scope": "both",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv("SHARED_URL", "http://real")
        p = _plugins(tmp_path)
        assert p.frontend_config() == {"shared_url": "http://real"}

    def test_container_only_scope_excluded_from_frontend_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "git-credential",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_GITHUB_OAUTH_CLIENT_ID": {
                                "description": "",
                                "default": "",
                                "scope": "container",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": ["KLANGK_GITHUB_OAUTH_CLIENT_ID"],
            },
        )
        monkeypatch.setenv("KLANGK_GITHUB_OAUTH_CLIENT_ID", "abc")
        p = _plugins(tmp_path)
        # container-only: in container_env, NOT in frontend_config.
        assert p.frontend_config() == {}
        assert p.container_env() == {"KLANGK_GITHUB_OAUTH_CLIENT_ID": "abc"}

    def test_default_used_when_env_unset(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "f",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "MY_KEY": {
                                "description": "",
                                "default": "fallback",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.delenv("MY_KEY", raising=False)
        p = _plugins(tmp_path)
        assert p.frontend_config() == {"my_key": "fallback"}

    def test_non_dict_feature_entry_skipped_in_frontend_config(self, tmp_path):
        # A feature entry that isn't a dict is skipped in frontend_config()
        # too (the guard mirrors feature_list's). Covers the type-safety path.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    "not-a-dict",
                    {
                        "name": "ok",
                        "config": {
                            "OK_KEY": {"default": "v", "scope": "frontend"}
                        },
                    },
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        assert p.frontend_config() == {"ok_key": "v"}

    def test_non_dict_config_block_ignored(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {"name": "bad", "config": "nope"},
                    {
                        "name": "ok",
                        "config": {
                            "OK_KEY": {"default": "v", "scope": "frontend"}
                        },
                    },
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        # bad feature's non-dict config is skipped; ok feature still resolves.
        assert p.frontend_config() == {"ok_key": "v"}

    def test_non_dict_spec_ignored(self, tmp_path):
        # A config entry whose value isn't a dict (e.g. a bare string) is
        # skipped, not crashed on.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "x",
                        "config": {"BAD_KEY": "not-a-dict"},
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        assert p.frontend_config() == {}

    def test_invalid_scope_defaults_to_container(self, tmp_path):
        # Mirrors the build's _CONTAINER_SCOPES defaulting — an unknown scope
        # is neither frontend nor both, so excluded from frontend_config.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "x",
                        "config": {
                            "X_KEY": {"default": "v", "scope": "bogus"}
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _plugins(tmp_path)
        assert p.frontend_config() == {}


class TestFeaturesEnable:
    """features_enable() forwards the KLANGK_FEATURES_ENABLE setting verbatim
    (the deploy's chosen active-feature list — canonical semantics, #1655)."""

    def test_unset_returns_none(self, tmp_path):
        p = _plugins(tmp_path)
        assert p.features_enable() is None

    def test_explicit_value_forwarded_verbatim(self, tmp_path):
        p = _plugins(
            tmp_path, env={"KLANGK_FEATURES_ENABLE": "celebrate,beep,soliplex"}
        )
        assert p.features_enable() == "celebrate,beep,soliplex"

    def test_single_value(self, tmp_path):
        p = _plugins(tmp_path, env={"KLANGK_FEATURES_ENABLE": "soliplex"})
        assert p.features_enable() == "soliplex"


class TestReconfigure:
    """reconfigure() re-reads the manifest on a SIGHUP settings reload
    (frontend_dir may have changed)."""

    def test_reconfigure_picks_up_new_manifest(self, tmp_path):
        # Start with no manifest → empty feature list.
        p = _plugins(tmp_path)
        assert p.feature_list() == []

        # Write a manifest, build a new app_state pointing at the same dir,
        # reconfigure → feature list reflects the new manifest.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "new-feature",
                        "version": "1.0.0",
                        "description": "",
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        new_app_state = types.SimpleNamespace(
            state=types.SimpleNamespace(
                settings=make_settings({"KLANGK_FRONTEND_DIR": str(tmp_path)})
            )
        )
        p.reconfigure(new_app_state)
        assert p.feature_list() == [
            {"name": "new-feature", "version": "1.0.0", "description": ""}
        ]
