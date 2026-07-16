"""Tests for plugins module."""

import json
import types

from klangk_backend import plugins
from _helpers import make_settings


def _make_plugin(tmp_path, name, config):
    """Create a plugin directory with a package.json containing klangk.config."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text(
        json.dumps({"name": f"@klangk/{name}", "klangk": {"config": config}})
    )


def _plugins(plugins_dir):
    """Build a fresh Plugins instance pointing at *plugins_dir* (#1451)."""
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(
            settings=make_settings({"KLANGK_PLUGINS_DIR": str(plugins_dir)})
        )
    )
    return plugins.Plugins(app_state)


class TestPluginConfig:
    def test_load_no_dir(self, tmp_path, monkeypatch):
        p = _plugins(tmp_path / "nonexistent")
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {}

    def test_load_empty_dir(self, tmp_path, monkeypatch):
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {}

    def test_load_dir_without_package_json(self, tmp_path, monkeypatch):
        (tmp_path / "no-manifest").mkdir()
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {}

    def test_load_plugin_without_config(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "no-config"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps({"name": "@klangk/no-config"})
        )
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {}

    def test_load_container_scope(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "my-plugin",
            {
                "MY_API_KEY": {
                    "description": "API key",
                    "default": "default-val",
                    "scope": "container",
                }
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.delenv("MY_API_KEY", raising=False)
        p.load()
        assert p.container_env() == {"MY_API_KEY": "default-val"}
        assert p.frontend_config() == {}

    def test_load_frontend_scope(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "my-plugin",
            {
                "MY_CLIENT_ID": {
                    "description": "OAuth client ID",
                    "default": "",
                    "scope": "frontend",
                }
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.setenv("MY_CLIENT_ID", "real-id")
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {"my_client_id": "real-id"}

    def test_load_both_scope(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "shared",
            {
                "SHARED_URL": {
                    "description": "URL needed everywhere",
                    "default": "http://default",
                    "scope": "both",
                }
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.setenv("SHARED_URL", "http://real")
        p.load()
        assert p.container_env() == {"SHARED_URL": "http://real"}
        assert p.frontend_config() == {"shared_url": "http://real"}

    def test_env_overrides_default(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "test",
            {
                "MY_VAR": {
                    "description": "",
                    "default": "fallback",
                    "scope": "container",
                }
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.setenv("MY_VAR", "from-env")
        p.load()
        assert p.container_env() == {"MY_VAR": "from-env"}

    def test_load_invalid_json(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text("not json")
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}
        assert p.frontend_config() == {}

    def test_multiple_plugins(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "alpha",
            {
                "A_KEY": {
                    "description": "",
                    "default": "a-default",
                    "scope": "container",
                }
            },
        )
        _make_plugin(
            tmp_path,
            "beta",
            {
                "B_KEY": {
                    "description": "",
                    "default": "",
                    "scope": "frontend",
                },
                "C_KEY": {
                    "description": "",
                    "default": "",
                    "scope": "both",
                },
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.setenv("B_KEY", "b-val")
        monkeypatch.setenv("C_KEY", "c-val")
        p.load()
        assert p.container_env() == {
            "A_KEY": "a-default",
            "C_KEY": "c-val",
        }
        assert p.frontend_config() == {
            "b_key": "b-val",
            "c_key": "c-val",
        }

    def test_non_dict_config_ignored(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "bad-config"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps({"name": "@klangk/bad", "klangk": {"config": "nope"}})
        )
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}

    def test_non_dict_spec_ignored(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "bad-spec"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": "@klangk/bad",
                    "klangk": {"config": {"MY_KEY": "not-a-dict"}},
                }
            )
        )
        p = _plugins(tmp_path)
        p.load()
        assert p.container_env() == {}

    def test_invalid_scope_defaults_to_container(self, tmp_path, monkeypatch):
        _make_plugin(
            tmp_path,
            "bad-scope",
            {
                "X_KEY": {
                    "description": "",
                    "default": "val",
                    "scope": "bogus",
                }
            },
        )
        p = _plugins(tmp_path)
        monkeypatch.delenv("X_KEY", raising=False)
        p.load()
        assert p.container_env() == {"X_KEY": "val"}
        assert p.frontend_config() == {}


class TestPluginList:
    def test_no_dir(self, tmp_path):
        p = _plugins(tmp_path / "nonexistent")
        assert p.plugin_list() == []

    def test_empty_dir(self, tmp_path):
        p = _plugins(tmp_path)
        assert p.plugin_list() == []

    def test_returns_metadata(self, tmp_path):
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": "my-plugin",
                    "version": "1.0.0",
                    "description": "A plugin",
                }
            )
        )
        p = _plugins(tmp_path)
        result = p.plugin_list()
        assert len(result) == 1
        assert result[0] == {
            "name": "my-plugin",
            "version": "1.0.0",
            "description": "A plugin",
        }

    def test_skips_invalid_json(self, tmp_path):
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text("not json")
        p = _plugins(tmp_path)
        assert p.plugin_list() == []

    def test_missing_fields_default_empty(self, tmp_path):
        plugin_dir = tmp_path / "minimal"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"name": "m"}))
        p = _plugins(tmp_path)
        result = p.plugin_list()
        assert result[0] == {
            "name": "minimal",
            "version": "",
            "description": "",
        }

    def test_skips_dir_without_package_json(self, tmp_path):
        (tmp_path / "no-manifest").mkdir()
        p = _plugins(tmp_path)
        assert p.plugin_list() == []

    def test_sorted_by_name(self, tmp_path):
        for name in ["zeta", "alpha", "mid"]:
            d = tmp_path / name
            d.mkdir()
            (d / "package.json").write_text(json.dumps({"name": name}))
        p = _plugins(tmp_path)
        names = [pl["name"] for pl in p.plugin_list()]
        assert names == ["alpha", "mid", "zeta"]
