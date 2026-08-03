"""Unit tests for the LiteLLM aggregator sidecar (#2046).

Tests the renderer (config.yaml generation), the settings validator
for ``KLANGKD_LLM_AGGREGATOR_MODELS``, and the watchdog container
lifecycle (``_watch``, ``_wait_for_exit``, ``_remove_container``,
``start``, ``stop``).
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


class _FakePodman:
    """Minimal podman stub for watchdog tests."""

    def __init__(self):
        self.calls = []
        self._run_results = []  # queue of (rc, out, err) to return from run()
        self._remove_error = None

    def queue_run(self, rc, out, err=""):
        self._run_results.append((rc, out, err))

    async def create_container(self, name, image, **kwargs):
        self.calls.append(("create", name, image, kwargs))
        return "fake-container-id-1234567890ab"

    async def start_container(self, container_id):
        self.calls.append(("start", container_id))

    async def remove_container(self, name):
        self.calls.append(("remove", name))
        if self._remove_error:
            raise self._remove_error

    async def run(self, args, **kwargs):
        self.calls.append(("run", args))
        if self._run_results:
            return self._run_results.pop(0)
        return (1, "", "")  # container not found by default


def _wd(settings, podman=None):
    """Build a LiteLLMWatchdog from settings (wrapped in a minimal mock app)."""
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            settings=settings,
            podman=podman or _FakePodman(),
        )
    )
    return LiteLLMWatchdog(app)


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
        assert s.llm_aggregator_port == 8996

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

    def test_drop_params_always_set(self):
        """Rendered config sets litellm_settings.drop_params so unsupported
        client params (e.g. max_completion_tokens on zai) are dropped
        silently instead of 400ing the whole request."""
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        r = _renderer(s)
        config = yaml.safe_load(r.render_config())
        assert config["litellm_settings"]["drop_params"] is True

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

    async def test_remove_container_success(self):
        podman = _FakePodman()
        s = make_settings({})
        wd = _wd(s, podman=podman)
        await wd._remove_container()
        assert ("remove", "klangk-litellm") in podman.calls

    async def test_remove_container_ignores_error(self):
        podman = _FakePodman()
        podman._remove_error = RuntimeError("no such container")
        s = make_settings({})
        wd = _wd(s, podman=podman)
        await wd._remove_container()
        assert ("remove", "klangk-litellm") in podman.calls

    async def test_wait_for_exit_returns_when_not_running(self):
        podman = _FakePodman()
        podman.queue_run(0, "false")
        s = make_settings({})
        wd = _wd(s, podman=podman)
        await wd._wait_for_exit()
        assert any(c[0] == "run" for c in podman.calls)

    async def test_wait_for_exit_returns_on_inspect_error(self):
        podman = _FakePodman()
        podman.queue_run(1, "")
        s = make_settings({})
        wd = _wd(s, podman=podman)
        await wd._wait_for_exit()

    async def test_wait_for_exit_returns_on_exception(self):
        podman = _FakePodman()
        s = make_settings({})
        wd = _wd(s, podman=podman)

        async def _exploding_run(args, **kwargs):
            raise OSError("podman gone")

        podman.run = _exploding_run
        await wd._wait_for_exit()

    async def test_wait_for_exit_polls_then_stops(self):
        """Polls while running, returns when _stopping is set."""
        podman = _FakePodman()
        s = make_settings({})
        wd = _wd(s, podman=podman)

        call_count = 0

        async def _counting_run(args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                wd._stopping = True
            return (0, "true", "")

        podman.run = _counting_run
        await wd._wait_for_exit()
        assert call_count >= 2

    async def test_watch_creates_starts_and_waits(self):
        """_watch creates a container, starts it, waits for exit, then
        stops when _stopping is set."""
        podman = _FakePodman()
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s, podman=podman)

        async def _fake_wait():
            wd._stopping = True

        wd._wait_for_exit = _fake_wait
        conf_path = wd._config_path()
        wd._renderer.write_config(conf_path)
        await wd._watch(conf_path)

        create_calls = [c for c in podman.calls if c[0] == "create"]
        assert len(create_calls) == 1
        assert create_calls[0][1] == "klangk-litellm"
        kwargs = create_calls[0][3]
        # Host port is llm_aggregator_port (default 8996); container port is
        # always 4000 (LiteLLM's internal port).
        assert kwargs["publish"] == [("127.0.0.1", 8996, 4000)]
        # #2062: --config is passed (else the mounted config is never loaded)
        # and no fatal empty DATABASE_URL is set.
        assert kwargs["command"] == [
            "--config",
            "/app/config.yaml",
            "--host",
            "0.0.0.0",
            "--port",
            "4000",
        ]
        assert "DATABASE_URL=" not in kwargs["env"]
        # Default (no master_key) -> no LITELLM_MASTER_KEY env (no-auth).
        assert not any(
            e.startswith("LITELLM_MASTER_KEY") for e in kwargs["env"]
        )

        start_calls = [c for c in podman.calls if c[0] == "start"]
        assert len(start_calls) == 1

    async def test_watch_passes_master_key_env_when_set(self):
        """When a master_key is configured, the sidecar env includes
        LITELLM_MASTER_KEY so LiteLLM enforces bearer auth (#2062)."""
        podman = _FakePodman()
        s = make_settings(
            {
                "KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx",
                "KLANGKD_LLM_AGGREGATOR_MASTER_KEY": "sk-master",
            }
        )
        wd = _wd(s, podman=podman)

        async def _fake_wait():
            wd._stopping = True

        wd._wait_for_exit = _fake_wait
        conf_path = wd._config_path()
        wd._renderer.write_config(conf_path)
        await wd._watch(conf_path)

        create_calls = [c for c in podman.calls if c[0] == "create"]
        env = create_calls[0][3]["env"]
        assert "LITELLM_MASTER_KEY=sk-master" in env
        assert "DATABASE_URL=" not in env

    async def test_watch_respawns_on_unexpected_exit(self):
        """_watch respawns when container exits unexpectedly."""
        podman = _FakePodman()
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s, podman=podman)

        exit_count = 0

        async def _fake_wait():
            nonlocal exit_count
            exit_count += 1
            if exit_count >= 2:
                wd._stopping = True

        wd._wait_for_exit = _fake_wait
        conf_path = wd._config_path()
        wd._renderer.write_config(conf_path)
        await wd._watch(conf_path)

        create_calls = [c for c in podman.calls if c[0] == "create"]
        assert len(create_calls) == 2

    async def test_watch_backoff_resets_after_successful_run(self):
        """Backoff resets to 1.0 after each successful container run,
        so repeated unexpected exits don't escalate the delay."""
        podman = _FakePodman()
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s, podman=podman)

        sleep_durations = []

        async def _tracking_sleep(duration):
            sleep_durations.append(duration)
            # Don't actually sleep.

        exit_count = 0

        async def _fake_wait():
            nonlocal exit_count
            exit_count += 1
            if exit_count >= 3:
                wd._stopping = True

        wd._wait_for_exit = _fake_wait
        conf_path = wd._config_path()
        wd._renderer.write_config(conf_path)

        orig_asyncio_sleep = asyncio.sleep
        asyncio.sleep = _tracking_sleep
        try:
            await wd._watch(conf_path)
        finally:
            asyncio.sleep = orig_asyncio_sleep

        # Each respawn should sleep 1.0 (reset), not 1.0 then 2.0.
        assert sleep_durations == [1.0, 1.0]

    async def test_watch_retries_on_create_failure(self):
        """_watch retries with backoff when create_container fails."""
        podman = _FakePodman()
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s, podman=podman)

        create_count = 0
        orig_create = podman.create_container

        async def _failing_create(name, image, **kwargs):
            nonlocal create_count
            create_count += 1
            if create_count == 1:
                raise RuntimeError("image pull failed")
            wd._stopping = True
            return await orig_create(name, image, **kwargs)

        podman.create_container = _failing_create

        async def _fake_wait():
            wd._stopping = True

        wd._wait_for_exit = _fake_wait
        conf_path = wd._config_path()
        wd._renderer.write_config(conf_path)
        await wd._watch(conf_path)
        assert create_count == 2

    async def test_start_noop_when_disabled(self, monkeypatch):
        """start() is a no-op when _KLANGKD_DISABLE_LITELLM is set."""
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s)
        monkeypatch.setenv("_KLANGKD_DISABLE_LITELLM", "1")
        await wd.start()
        assert wd._task is None

    async def test_stop_cancels_running_task(self, monkeypatch):
        """stop() cancels the watch task and removes the container."""
        podman = _FakePodman()
        s = make_settings(
            {"KLANGKD_LLM_AGGREGATOR_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        wd = _wd(s, podman=podman)
        monkeypatch.delenv("_KLANGKD_DISABLE_LITELLM", raising=False)

        async def _forever():
            await asyncio.sleep(999)

        wd._task = asyncio.create_task(_forever())
        await wd.stop()
        assert wd._stopping is True
        assert wd._task is None
        assert any(c[0] == "remove" for c in podman.calls)

    async def test_stop_noop_when_no_task(self):
        """stop() is safe when no task was started."""
        s = make_settings({})
        wd = _wd(s)
        await wd.stop()
        assert wd._stopping is True
        assert wd._task is None
