"""Unit tests for the in-process LLM router (#2071)."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

from klangk.llm_router import LLMRouter, _normalize_dict_entry


class TestLLMRouterStringEntries:
    def test_single_model(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
        names = router.get_model_names()
        assert "gpt-4o" in names

    def test_multiple_models(self):
        router = LLMRouter(
            [
                "openai/gpt-4o::sk-xxx",
                "anthropic/claude-sonnet-4::sk-ant-xxx",
            ]
        )
        names = router.get_model_names()
        assert "gpt-4o" in names
        assert "claude-sonnet-4" in names

    def test_local_ollama(self):
        router = LLMRouter(["ollama/llama3:http://localhost:11434:"])
        names = router.get_model_names()
        assert "llama3" in names

    def test_vllm_model(self):
        router = LLMRouter(
            ["hosted_vllm/RedHatAI/Qwen3.6-35B:http://bizon:11430:"]
        )
        names = router.get_model_names()
        assert "RedHatAI/Qwen3.6-35B" in names

    def test_default_api_key_applied(self):
        router = LLMRouter(
            ["openai/gpt-4o::"],
            default_api_key="sk-default",
        )
        model_list = router.get_model_list()
        assert len(model_list) == 1
        params = model_list[0]["litellm_params"]
        assert params["api_key"] == "sk-default"

    def test_explicit_key_not_overridden_by_default(self):
        router = LLMRouter(
            ["openai/gpt-4o::sk-explicit"],
            default_api_key="sk-default",
        )
        model_list = router.get_model_list()
        params = model_list[0]["litellm_params"]
        assert params["api_key"] == "sk-explicit"

    def test_get_model_list_structure(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
        model_list = router.get_model_list()
        assert len(model_list) == 1
        entry = model_list[0]
        assert "model_name" in entry
        assert "litellm_params" in entry
        assert entry["litellm_params"]["model"] == "openai/gpt-4o"


class TestLLMRouterDictEntries:
    def test_snake_case_dict(self):
        router = LLMRouter(
            [
                {
                    "model_name": "gpt-4",
                    "litellm_params": {
                        "model": "openai/gpt-4o",
                        "api_key": "sk-xxx",
                    },
                }
            ]
        )
        assert "gpt-4" in router.get_model_names()
        params = router.get_model_list()[0]["litellm_params"]
        assert params["model"] == "openai/gpt-4o"
        assert params["api_key"] == "sk-xxx"

    def test_kebab_case_dict(self):
        router = LLMRouter(
            [
                {
                    "model-name": "local-llm",
                    "litellm-params": {
                        "model": "ollama/llama3",
                        "api-base": "http://localhost:11434",
                        "api-key": "dummy",
                    },
                }
            ]
        )
        assert "local-llm" in router.get_model_names()
        params = router.get_model_list()[0]["litellm_params"]
        assert params["model"] == "ollama/llama3"
        assert params["api_base"] == "http://localhost:11434"
        assert params["api_key"] == "dummy"

    def test_mixed_string_and_dict(self):
        router = LLMRouter(
            [
                "openai/gpt-4o::sk-xxx",
                {
                    "model_name": "local-llm",
                    "litellm_params": {
                        "model": "ollama/llama3",
                        "api_base": "http://localhost:11434",
                    },
                },
            ]
        )
        names = router.get_model_names()
        assert "gpt-4o" in names
        assert "local-llm" in names

    def test_default_api_key_applied_to_dict(self):
        router = LLMRouter(
            [
                {
                    "model_name": "gpt-4",
                    "litellm_params": {"model": "openai/gpt-4o"},
                }
            ],
            default_api_key="sk-default",
        )
        params = router.get_model_list()[0]["litellm_params"]
        assert params["api_key"] == "sk-default"

    def test_dict_explicit_key_not_overridden(self):
        router = LLMRouter(
            [
                {
                    "model_name": "gpt-4",
                    "litellm_params": {
                        "model": "openai/gpt-4o",
                        "api_key": "sk-explicit",
                    },
                }
            ],
            default_api_key="sk-default",
        )
        params = router.get_model_list()[0]["litellm_params"]
        assert params["api_key"] == "sk-explicit"


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


class TestLLMRouterReconfigure:
    def test_reconfigure_replaces_models(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
        assert "gpt-4o" in router.get_model_names()

        router.reconfigure(["anthropic/claude-sonnet-4::sk-ant-xxx"])
        names = router.get_model_names()
        assert "claude-sonnet-4" in names
        assert "gpt-4o" not in names

    def test_reconfigure_with_dicts(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
        router.reconfigure(
            [
                {
                    "model_name": "local",
                    "litellm_params": {
                        "model": "ollama/llama3",
                        "api_base": "http://localhost:11434",
                    },
                }
            ]
        )
        names = router.get_model_names()
        assert "local" in names
        assert "gpt-4o" not in names

    def test_reconfigure_updates_default_key(self):
        router = LLMRouter(
            ["openai/gpt-4o::"],
            default_api_key="sk-old",
        )
        router.reconfigure(
            ["openai/gpt-4o::"],
            default_api_key="sk-new",
        )
        model_list = router.get_model_list()
        assert model_list[0]["litellm_params"]["api_key"] == "sk-new"


class TestLLMRouterCompletion:
    async def test_acompletion_delegates_to_router(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
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
            mock.assert_called_once_with(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
