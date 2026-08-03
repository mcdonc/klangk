"""Unit tests for the in-process LLM router (#2071)."""

from unittest.mock import AsyncMock, patch


from klangk.llm_router import LLMRouter


class TestLLMRouterInit:
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


class TestLLMRouterReconfigure:
    def test_reconfigure_replaces_models(self):
        router = LLMRouter(["openai/gpt-4o::sk-xxx"])
        assert "gpt-4o" in router.get_model_names()

        router.reconfigure(["anthropic/claude-sonnet-4::sk-ant-xxx"])
        names = router.get_model_names()
        assert "claude-sonnet-4" in names
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
