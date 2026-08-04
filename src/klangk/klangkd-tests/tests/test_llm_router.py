"""Unit tests for the in-process LLM router (#2071, #2072)."""

import os
import tempfile
import types
from unittest.mock import AsyncMock, patch

from klangk.llm_router import (
    LLMRouter,
    _normalize_dict_entry,
    parse_model_entry,
)
from _helpers import make_settings


def _app(extra_env=None):
    """Build a minimal mock app with settings for LLMRouter."""
    env = dict(extra_env or {})
    settings = make_settings(env=env)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )


class TestLLMRouterSubsystem:
    def test_no_models_configured(self):
        router = LLMRouter(_app())
        assert not router.active
        assert router.get_model_names() == []
        assert router.get_model_list() == []

    def test_with_string_models(self):
        router = LLMRouter(
            _app(
                {
                    "KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx,ollama/llama3:http://localhost:11434:"
                }
            )
        )
        assert router.active
        names = router.get_model_names()
        assert "gpt-4o" in names
        assert "llama3" in names

    def test_default_api_key_from_settings(self):
        router = LLMRouter(
            _app(
                {
                    "KLANGKD_LLM_MODELS": "openai/gpt-4o::",
                    "KLANGKD_LLM_API_KEY": "sk-default",
                }
            )
        )
        params = router.get_model_list()[0]["litellm_params"]
        assert params["api_key"] == "sk-default"

    def test_reconfigure_adds_models(self):
        app = _app()
        router = LLMRouter(app)
        assert not router.active

        new_app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router.reconfigure(new_app)
        assert router.active
        assert "gpt-4o" in router.get_model_names()

    def test_reconfigure_removes_models(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        assert router.active

        router.reconfigure(_app())
        assert not router.active

    def test_reconfigure_replaces_models(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        assert "gpt-4o" in router.get_model_names()

        new_app = _app(
            {"KLANGKD_LLM_MODELS": "anthropic/claude-sonnet-4::sk-ant-xxx"}
        )
        router.reconfigure(new_app)
        names = router.get_model_names()
        assert "claude-sonnet-4" in names
        assert "gpt-4o" not in names


class TestLLMRouterDictEntries:
    def test_dict_entries_via_subsystem(self):
        app = _app()
        # Inject dict entries directly (simulates YAML config).
        app.state.settings.llm_models = [
            {
                "model_name": "gpt-4",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_key": "sk-xxx",
                },
            }
        ]
        router = LLMRouter(app)
        assert router.active
        assert "gpt-4" in router.get_model_names()
        params = router.get_model_list()[0]["litellm_params"]
        assert params["model"] == "openai/gpt-4o"


class TestNormalizeDictEntry:
    def test_kebab_to_snake_top_level(self):
        result = _normalize_dict_entry(
            {
                "model-name": "test",
                "litellm-params": {"model": "openai/gpt-4o"},
            }
        )
        assert "model_name" in result
        assert "litellm_params" in result

    def test_kebab_to_snake_params(self):
        result = _normalize_dict_entry(
            {
                "model_name": "test",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api-key": "sk-xxx",
                    "api-base": "http://example.com",
                },
            }
        )
        params = result["litellm_params"]
        assert "api_key" in params
        assert "api_base" in params

    def test_file_indirection_on_api_key(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".key", delete=False
        ) as f:
            f.write("sk-from-file\n")
            f.flush()
            try:
                result = _normalize_dict_entry(
                    {
                        "model_name": "test",
                        "litellm_params": {
                            "model": "openai/gpt-4o",
                            "api_key": f"file:{f.name}",
                        },
                    }
                )
                assert result["litellm_params"]["api_key"] == "sk-from-file"
            finally:
                os.unlink(f.name)

    def test_cmd_indirection_on_api_key(self):
        result = _normalize_dict_entry(
            {
                "model_name": "test",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_key": "cmd:echo sk-from-cmd",
                },
            }
        )
        assert result["litellm_params"]["api_key"] == "sk-from-cmd"

    def test_file_indirection_on_api_base(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".url", delete=False
        ) as f:
            f.write("http://secret-host:8080\n")
            f.flush()
            try:
                result = _normalize_dict_entry(
                    {
                        "model_name": "test",
                        "litellm_params": {
                            "model": "openai/gpt-4o",
                            "api_base": f"file:{f.name}",
                        },
                    }
                )
                assert (
                    result["litellm_params"]["api_base"]
                    == "http://secret-host:8080"
                )
            finally:
                os.unlink(f.name)

    def test_params_alias_for_litellm_params(self):
        result = _normalize_dict_entry(
            {
                "model_name": "test",
                "params": {
                    "model": "openai/gpt-4o",
                    "api_key": "sk-xxx",
                },
            }
        )
        assert "litellm_params" in result
        assert result["litellm_params"]["model"] == "openai/gpt-4o"
        assert result["litellm_params"]["api_key"] == "sk-xxx"

    def test_non_indirect_keys_left_alone(self):
        result = _normalize_dict_entry(
            {
                "model_name": "test",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "timeout": 30,
                },
            }
        )
        assert result["litellm_params"]["timeout"] == 30
        assert result["litellm_params"]["model"] == "openai/gpt-4o"


class TestLLMRouterCompletion:
    async def test_acompletion_delegates_to_router(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        mock_response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(
            router._router, "acompletion", new_callable=AsyncMock
        ) as mock:
            mock.return_value = mock_response
            result = await router.acompletion(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert result == mock_response

    async def test_acompletion_raises_when_not_configured(self):
        router = LLMRouter(_app())
        with __import__("pytest").raises(RuntimeError, match="not configured"):
            await router.acompletion(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
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

    def test_no_colons(self):
        result = parse_model_entry("openai/gpt-4o")
        assert result["model_name"] == "gpt-4o"
        assert result["litellm_params"]["model"] == "openai/gpt-4o"

    def test_single_colon_uses_rest_as_base(self):
        result = parse_model_entry("openai/gpt-4o:somebase")
        assert result["litellm_params"]["api_base"] == "somebase"

    def test_no_provider_prefix(self):
        result = parse_model_entry("llama3:http://localhost:11434:")
        assert result["model_name"] == "llama3"
        assert result["litellm_params"]["api_base"] == "http://localhost:11434"

    def test_provider_default_base_url(self):
        result = parse_model_entry("openai/gpt-4o::sk-xxx")
        assert (
            result["litellm_params"]["api_base"] == "https://api.openai.com/v1"
        )
        assert result["litellm_params"]["api_key"] == "sk-xxx"
