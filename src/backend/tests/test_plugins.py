"""Tests for plugins module."""

import json

from klangk_backend import plugins


def _make_plugin(tmp_path, name, config):
    """Create a plugin directory with a package.json containing klangk.config."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text(
        json.dumps({"name": f"@klangk/{name}", "klangk": {"config": config}})
    )


class TestPluginConfig:
    def test_load_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            plugins, "_PLUGINS_DIR", str(tmp_path / "nonexistent")
        )
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {}

    def test_load_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {}

    def test_load_dir_without_package_json(self, tmp_path, monkeypatch):
        (tmp_path / "no-manifest").mkdir()
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {}

    def test_load_plugin_without_config(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "no-config"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps({"name": "@klangk/no-config"})
        )
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.delenv("MY_API_KEY", raising=False)
        plugins.load()
        assert plugins.container_env() == {"MY_API_KEY": "default-val"}
        assert plugins.frontend_config() == {}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.setenv("MY_CLIENT_ID", "real-id")
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {"my_client_id": "real-id"}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.setenv("SHARED_URL", "http://real")
        plugins.load()
        assert plugins.container_env() == {"SHARED_URL": "http://real"}
        assert plugins.frontend_config() == {"shared_url": "http://real"}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.setenv("MY_VAR", "from-env")
        plugins.load()
        assert plugins.container_env() == {"MY_VAR": "from-env"}

    def test_load_invalid_json(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text("not json")
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}
        assert plugins.frontend_config() == {}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.setenv("B_KEY", "b-val")
        monkeypatch.setenv("C_KEY", "c-val")
        plugins.load()
        assert plugins.container_env() == {
            "A_KEY": "a-default",
            "C_KEY": "c-val",
        }
        assert plugins.frontend_config() == {
            "b_key": "b-val",
            "c_key": "c-val",
        }

    def test_non_dict_config_ignored(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "bad-config"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(
            json.dumps({"name": "@klangk/bad", "klangk": {"config": "nope"}})
        )
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        plugins.load()
        assert plugins.container_env() == {}

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
        monkeypatch.setattr(plugins, "_PLUGINS_DIR", str(tmp_path))
        monkeypatch.delenv("X_KEY", raising=False)
        plugins.load()
        assert plugins.container_env() == {"X_KEY": "val"}
        assert plugins.frontend_config() == {}
