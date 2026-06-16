"""Tests for plugin_config module."""

import json

from klangk_backend import plugin_config


class TestCollectPluginConfig:
    def test_load_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            plugin_config, "_CONFIG_PATH", str(tmp_path / "missing.json")
        )
        plugin_config.load()
        assert plugin_config.container_env() == {}
        assert plugin_config.frontend_config() == {}

    def test_load_empty_file(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        plugin_config.load()
        assert plugin_config.container_env() == {}
        assert plugin_config.frontend_config() == {}

    def test_load_container_scope(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "MY_API_KEY": {
                        "plugin": "my-plugin",
                        "description": "API key",
                        "default": "default-val",
                        "scope": "container",
                    }
                }
            )
        )
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        monkeypatch.delenv("MY_API_KEY", raising=False)
        plugin_config.load()
        assert plugin_config.container_env() == {"MY_API_KEY": "default-val"}
        assert plugin_config.frontend_config() == {}

    def test_load_frontend_scope(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "MY_CLIENT_ID": {
                        "plugin": "my-plugin",
                        "description": "OAuth client ID",
                        "default": "",
                        "scope": "frontend",
                    }
                }
            )
        )
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        monkeypatch.setenv("MY_CLIENT_ID", "real-id")
        plugin_config.load()
        assert plugin_config.container_env() == {}
        assert plugin_config.frontend_config() == {"my_client_id": "real-id"}

    def test_load_both_scope(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "SHARED_URL": {
                        "plugin": "shared",
                        "description": "URL needed everywhere",
                        "default": "http://default",
                        "scope": "both",
                    }
                }
            )
        )
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        monkeypatch.setenv("SHARED_URL", "http://real")
        plugin_config.load()
        assert plugin_config.container_env() == {"SHARED_URL": "http://real"}
        assert plugin_config.frontend_config() == {"shared_url": "http://real"}

    def test_env_overrides_default(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "MY_VAR": {
                        "plugin": "test",
                        "description": "",
                        "default": "fallback",
                        "scope": "container",
                    }
                }
            )
        )
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        monkeypatch.setenv("MY_VAR", "from-env")
        plugin_config.load()
        assert plugin_config.container_env() == {"MY_VAR": "from-env"}

    def test_load_invalid_json(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text("not json")
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        plugin_config.load()
        assert plugin_config.container_env() == {}
        assert plugin_config.frontend_config() == {}

    def test_multiple_keys(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "A_KEY": {
                        "plugin": "a",
                        "description": "",
                        "default": "a-default",
                        "scope": "container",
                    },
                    "B_KEY": {
                        "plugin": "b",
                        "description": "",
                        "default": "",
                        "scope": "frontend",
                    },
                    "C_KEY": {
                        "plugin": "c",
                        "description": "",
                        "default": "",
                        "scope": "both",
                    },
                }
            )
        )
        monkeypatch.setattr(plugin_config, "_CONFIG_PATH", str(config_path))
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.setenv("B_KEY", "b-val")
        monkeypatch.setenv("C_KEY", "c-val")
        plugin_config.load()
        assert plugin_config.container_env() == {
            "A_KEY": "a-default",
            "C_KEY": "c-val",
        }
        assert plugin_config.frontend_config() == {
            "b_key": "b-val",
            "c_key": "c-val",
        }
