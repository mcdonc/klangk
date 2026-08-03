"""Unit tests for the LiteLLM aggregator sidecar (#2046).

Tests the renderer (config.yaml generation) and the settings validator
for ``KLANGKD_LLM_AGGREGATOR_MODELS``.  Runtime supervision (container
lifecycle) is covered implicitly by the watchdog pattern shared with
``ProxyWatchdog`` — the container spawn loop is ``# pragma: no cover``
like its nginx counterpart.
"""

import asyncio
import os
import tempfile
import types

import pytest
import yaml

from klangk.litellm import (
    LiteLLMRenderer,
    LiteLLMWatchdog,
    parse_model_entry,
)
from _helpers import make_settings


def _renderer(settings):
    """Wrap settings in a minimal mock app and build a LiteLLMRenderer."""
    return LiteLLMRenderer(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


def _wd(settings):
    """Build a LiteLLMWatchdog from settings (wrapped in a minimal mock app)."""
    return LiteLLMWatchdog(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


class TestParseModelEntry:
    def test_full_entry(self):
        result = parse_model_entry(
            "openai/gpt-4o:https://api.openai.com/v1:sk-xxx"
        )
        assert result["model_name"] == "gpt-4o"
        assert result["litellm_params"]["model"] == "openai/gpt-4o"
        assert (
            result["litellm_params"]["api_base"] == "https://api.openai.com/v1"
        )
        assert result["litellm_params"]["api_key"] == "sk-xxx"

    def test_empty_api_base_uses_provider_default(self):
        result = parse_model_entry("openai/gpt-4o::sk-xxx")
        assert (
            result["litellm_params"]["api_base"] == "https://api.openai.com/v1"
        )
        assert result["litellm_params"]["api_key"] == "sk-xxx"

    def test_empty_api_key(self):
        result = parse_model_entry("ollama/llama3:http://gpu:11434:")
        assert result["model_name"] == "llama3"
        assert result["litellm_params"]["api_base"] == "http://gpu:11434"
        assert "api_key" not in result["litellm_params"]

    def test_anthropic_default(self):
        result = parse_model_entry("anthropic/claude-sonnet-4::sk-ant-xxx")
        assert (
            result["litellm_params"]["api_base"]
            == "https://api.anthropic.com/v1"
        )

    def test_unknown_provider_no_api_base(self):
        result = parse_model_entry("custom/model::")
        assert "api_base" not in result["litellm_params"]

    def test_no_provider_slash(self):
        result = parse_model_entry("gpt-4o:https://api.openai.com/v1:sk-xxx")
        assert result["model_name"] == "gpt-4o"

    def test_url_with_port(self):
        """URLs with ports (extra colons) are handled correctly."""
        result = parse_model_entry("ollama/llama3:http://gpu:11434/v1:sk-key")
        assert result["litellm_params"]["api_base"] == "http://gpu:11434/v1"
        assert result["litellm_params"]["api_key"] == "sk-key"

    def test_no_colon_at_all(self):
        """Edge case: bare model with no colons (would be rejected by
        the settings validator, but parse_model_entry handles it)."""
        result = parse_model_entry("gpt-4o")
        assert result["model_name"] == "gpt-4o"
        assert "api_base" not in result["litellm_params"]
        assert "api_key" not in result["litellm_params"]

    def test_single_colon(self):
        """Edge case: one colon with a plain value (no URL).  The settings
        validator rejects < 2 colons, so this path is defensive only."""
        result = parse_model_entry("openai/gpt-4o:some-value")
        assert result["model_name"] == "gpt-4o"
        # With one colon, the rest has no rfind split — treated as api_base.
        assert result["litellm_params"]["api_base"] == "some-value"


class TestSettingsValidator:
    def test_none_accepted(self):
        s = make_settings({})
        assert s.llm_aggregator_models is None

    def test_empty_string_becomes_none(self):
        s = make_settings({"KLANGKD_LLM_AGGREGATOR_MODELS": ""})
        assert s.llm_aggregator_models is None

    def test_single_entry(self):
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        assert s.llm_aggregator_models == ["openai/gpt-4o::sk-xxx"]

    def test_comma_separated(self):
        s = make_settings(
            {
                "KLANGKD_LLM_AGGREGATOR_MODELS": (
                    "openai/gpt-4o::sk-xxx,anthropic/claude-sonnet-4::sk-ant"
                )
            }
        )
        assert len(s.llm_aggregator_models) == 2

    def test_rejects_malformed_entry(self):
        with pytest.raises(Exception, match="two colons"):
            make_settings({"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o"})

    def test_default_port(self):
        s = make_settings({})
        assert s.llm_aggregator_port == 4000

    def test_custom_port(self):
        s = make_settings({"KLANGKD_LLM_AGGREGATOR_PORT": "5000"})
        assert s.llm_aggregator_port == 5000

    def test_list_from_yaml_config(self):
        """YAML config file delivers a native list (not comma-separated)."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump({"llm-aggregator-models": ["openai/gpt-4o::sk-xxx"]}, f)
            f.flush()
            s = make_settings({}, config_file=f.name)
        os.unlink(f.name)
        assert s.llm_aggregator_models == ["openai/gpt-4o::sk-xxx"]

    def test_dict_entry_from_yaml(self):
        """YAML dict entries with id/base-url/api-key are accepted."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "llm-aggregator-models": [
                        {
                            "id": "openai/gpt-4o",
                            "api-key": "sk-xxx",
                        }
                    ]
                },
                f,
            )
            f.flush()
            s = make_settings({}, config_file=f.name)
        os.unlink(f.name)
        assert s.llm_aggregator_models == ["openai/gpt-4o::sk-xxx"]

    def test_dict_entry_with_base_url(self):
        """Dict entry with explicit base-url."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "llm-aggregator-models": [
                        {
                            "id": "ollama/llama3",
                            "base-url": "http://gpu:11434",
                            "api-key": "",
                        }
                    ]
                },
                f,
            )
            f.flush()
            s = make_settings({}, config_file=f.name)
        os.unlink(f.name)
        assert s.llm_aggregator_models == ["ollama/llama3:http://gpu:11434:"]

    def test_dict_entry_missing_id_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {"llm-aggregator-models": [{"api-key": "sk-xxx"}]},
                f,
            )
            f.flush()
            with pytest.raises(Exception, match="'id'"):
                make_settings({}, config_file=f.name)
        os.unlink(f.name)

    def test_dict_entry_cmd_indirection(self, tmp_path):
        """api-key with cmd: prefix is resolved."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "llm-aggregator-models": [
                        {
                            "id": "openai/gpt-4o",
                            "api-key": "cmd:echo resolved-key",
                        }
                    ]
                },
                f,
            )
            f.flush()
            s = make_settings({}, config_file=f.name)
        os.unlink(f.name)
        assert s.llm_aggregator_models == ["openai/gpt-4o::resolved-key"]

    def test_dict_entry_file_indirection(self, tmp_path):
        """api-key with file: prefix is resolved."""
        key_file = tmp_path / "key.txt"
        key_file.write_text("sk-from-file\n")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(
                {
                    "llm-aggregator-models": [
                        {
                            "id": "openai/gpt-4o",
                            "api-key": f"file:{key_file}",
                        }
                    ]
                },
                f,
            )
            f.flush()
            s = make_settings({}, config_file=f.name)
        os.unlink(f.name)
        assert s.llm_aggregator_models == ["openai/gpt-4o::sk-from-file"]

    def test_default_image(self):
        s = make_settings({})
        assert "litellm" in s.llm_aggregator_image


class TestLiteLLMRenderer:
    def test_no_models_returns_empty(self):
        s = make_settings({})
        r = _renderer(s)
        assert r.render_config() == ""

    def test_single_model_renders_yaml(self):
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        r = _renderer(s)
        config = yaml.safe_load(r.render_config())
        assert len(config["model_list"]) == 1
        entry = config["model_list"][0]
        assert entry["model_name"] == "gpt-4o"
        assert entry["litellm_params"]["model"] == "openai/gpt-4o"

    def test_multiple_models(self):
        s = make_settings(
            {
                "KLANGKD_LLM_AGGREGATOR_MODELS": (
                    "openai/gpt-4o::sk-xxx,anthropic/claude-sonnet-4::sk-ant"
                )
            }
        )
        r = _renderer(s)
        config = yaml.safe_load(r.render_config())
        assert len(config["model_list"]) == 2
        names = [e["model_name"] for e in config["model_list"]]
        assert "gpt-4o" in names
        assert "claude-sonnet-4" in names

    def test_master_key_in_config(self):
        s = make_settings(
            {
                "KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx",
                "KLANGKD_LLM_AGGREGATOR_MASTER_KEY": "sk-master",
            }
        )
        r = _renderer(s)
        config = yaml.safe_load(r.render_config())
        assert config["general_settings"]["master_key"] == "sk-master"

    def test_no_master_key_no_general_settings(self):
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        r = _renderer(s)
        config = yaml.safe_load(r.render_config())
        assert "general_settings" not in config

    def test_write_config_creates_file(self):
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        r = _renderer(s)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            r.write_config(path)
            assert os.path.exists(path)
            # File should be mode 0o600 (secrets protection).
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600
            with open(path) as f:
                config = yaml.safe_load(f.read())
            assert len(config["model_list"]) == 1

    def test_write_config_noop_when_no_models(self):
        s = make_settings({})
        r = _renderer(s)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            r.write_config(path)
            assert not os.path.exists(path)


class TestLiteLLMWatchdog:
    def test_construction(self):
        s = make_settings({})
        wd = _wd(s)
        assert wd._task is None
        assert wd._stopping is False

    def test_reconfigure_flags_reload_on_change(self):
        s1 = make_settings({})
        wd = _wd(s1)
        s2 = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        app2 = types.SimpleNamespace(state=types.SimpleNamespace(settings=s2))
        wd.reconfigure(app2)
        assert wd._pending_reload is True

    def test_reconfigure_no_flag_when_unchanged(self):
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s)
        app2 = types.SimpleNamespace(state=types.SimpleNamespace(settings=s))
        wd.reconfigure(app2)
        assert wd._pending_reload is False

    async def test_start_noop_when_no_models(self):
        s = make_settings({})
        wd = _wd(s)
        await wd.start()
        assert wd._task is None

    def test_config_path(self):
        s = make_settings({})
        wd = _wd(s)
        path = wd._config_path()
        assert path.endswith("litellm-config.yaml")
        assert s.state_dir in path

    async def test_start_creates_task_when_models_configured(
        self, monkeypatch
    ):
        """When models are configured and proxy is not disabled, start()
        writes config and creates a watch task (stubbed)."""
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s)
        monkeypatch.delenv("_KLANGKD_DISABLE_LITELLM", raising=False)

        watch_called = {}

        async def _fake_watch(conf_path):
            watch_called["conf"] = conf_path

        monkeypatch.setattr(wd, "_watch", _fake_watch)
        await wd.start()
        assert wd._task is not None
        # Let the task run so _fake_watch executes.
        await asyncio.sleep(0)
        assert "conf" in watch_called
        assert watch_called["conf"].endswith("litellm-config.yaml")

    async def test_apply_pending_reload_noop_when_not_flagged(self):
        s = make_settings({})
        wd = _wd(s)
        remove_called = []

        async def _fake_remove():
            remove_called.append(True)

        wd._remove_container = _fake_remove
        await wd.apply_pending_reload()
        assert not remove_called

    async def test_apply_pending_reload_disable_branch(self):
        """When models become empty, stop the task and remove container."""
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s)

        # Simulate a running task.
        async def _forever():
            await asyncio.sleep(999)

        wd._task = asyncio.create_task(_forever())
        wd._stopping = False

        # Now reconfigure with empty models.
        s_empty = make_settings({})
        app_empty = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=s_empty)
        )
        wd.reconfigure(app_empty)
        assert wd._pending_reload is True

        remove_called = []

        async def _fake_remove():
            remove_called.append(True)

        wd._remove_container = _fake_remove
        await wd.apply_pending_reload()
        assert wd._stopping is True
        assert wd._task is None
        assert remove_called

    async def test_apply_pending_reload_restart_branch(self, monkeypatch):
        """When models change, cancel old task and start a new one."""
        s1 = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s1)

        # Simulate a running task.
        async def _forever():
            await asyncio.sleep(999)

        wd._task = asyncio.create_task(_forever())
        old_task = wd._task

        # Reconfigure with different models.
        s2 = make_settings(
            {
                "KLANGKD_LLM_AGGREGATOR_MODELS": "anthropic/claude-sonnet-4::sk-ant"
            }
        )
        app2 = types.SimpleNamespace(state=types.SimpleNamespace(settings=s2))
        wd.reconfigure(app2)

        remove_called = []

        async def _fake_remove():
            remove_called.append(True)

        watch_called = {}

        async def _fake_watch(conf_path):
            watch_called["conf"] = conf_path

        wd._remove_container = _fake_remove
        monkeypatch.setattr(wd, "_watch", _fake_watch)
        await wd.apply_pending_reload()
        assert old_task.cancelled()
        assert wd._stopping is False
        assert wd._task is not None
        assert remove_called
        await asyncio.sleep(0)
        assert "conf" in watch_called
