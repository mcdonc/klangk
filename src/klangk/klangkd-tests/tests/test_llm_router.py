"""Unit tests for the in-process LLM router (#2071, #2072)."""

import asyncio
import logging
import os
import tempfile
import types
from unittest.mock import AsyncMock, patch

import httpx

from klangk import llm_router as llm_router_mod
from klangk.llm_router import (
    LLMRouter,
    is_passthrough,
    normalize_dict_entry,
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
        result = normalize_dict_entry(
            {
                "model-name": "test",
                "litellm-params": {"model": "openai/gpt-4o"},
            }
        )
        assert "model_name" in result
        assert "litellm_params" in result

    def test_kebab_to_snake_params(self):
        result = normalize_dict_entry(
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
                result = normalize_dict_entry(
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
        result = normalize_dict_entry(
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
                result = normalize_dict_entry(
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
        result = normalize_dict_entry(
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
        result = normalize_dict_entry(
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

    async def test_acompletion_empty_model(self):
        """Empty model string routes to the first configured model."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        mock_response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(
            router._router, "acompletion", new_callable=AsyncMock
        ) as mock:
            mock.return_value = mock_response
            await router.acompletion(
                model="",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert mock.call_args.kwargs["model"] == "gpt-4o"

    async def test_acompletion_unknown_model_falls_back(self):
        """An unrecognized model name falls back to the first model."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        mock_response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(
            router._router, "acompletion", new_callable=AsyncMock
        ) as mock:
            mock.return_value = mock_response
            await router.acompletion(
                model="gemma4:31b",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert mock.call_args.kwargs["model"] == "gpt-4o"

    async def test_acompletion_missing_model(self):
        """No model kwarg routes to the first configured model."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        mock_response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(
            router._router, "acompletion", new_callable=AsyncMock
        ) as mock:
            mock.return_value = mock_response
            await router.acompletion(
                messages=[{"role": "user", "content": "hi"}],
            )
            assert mock.call_args.kwargs["model"] == "gpt-4o"

    async def test_acompletion_fallback_raises_when_no_models(self):
        """Unknown model with an empty model list raises."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        router._router.set_model_list([])
        with __import__("pytest").raises(
            RuntimeError, match="no models configured"
        ):
            await router.acompletion(
                model="unknown",
                messages=[{"role": "user", "content": "hi"}],
            )


class TestPassthrough:
    def test_is_passthrough_single_wildcard(self):
        ml = [{"model_name": "*", "litellm_params": {}}]
        assert is_passthrough(ml)

    def test_not_passthrough_named_wildcard(self):
        """Only model_name='*' triggers passthrough, not 'openai/*'."""
        ml = [{"model_name": "openai/*", "litellm_params": {}}]
        assert not is_passthrough(ml)

    def test_not_passthrough_star_in_name(self):
        """A name containing '*' but not exactly '*' is not passthrough."""
        ml = [{"model_name": "my*model", "litellm_params": {}}]
        assert not is_passthrough(ml)

    def test_not_passthrough_no_wildcard(self):
        ml = [
            {
                "model_name": "gpt-4o",
                "litellm_params": {"model": "openai/gpt-4o"},
            }
        ]
        assert not is_passthrough(ml)

    def test_not_passthrough_multiple_entries(self):
        ml = [
            {"model_name": "*", "litellm_params": {}},
            {
                "model_name": "llama",
                "litellm_params": {"model": "ollama/llama3"},
            },
        ]
        assert not is_passthrough(ml)

    def test_passthrough_mode_active(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://localhost:11434",
                    "api_key": "dummy",
                },
            }
        ]
        router = LLMRouter(app)
        assert router.active
        assert router.passthrough
        assert router._passthrough_base == "http://localhost:11434"
        assert router._passthrough_key == "dummy"

    def test_passthrough_mode_inactive_for_explicit_models(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        assert router.active
        assert not router.passthrough

    def test_reconfigure_to_passthrough(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        assert not router.passthrough

        new_app = _app()
        new_app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://localhost:11434",
                },
            }
        ]
        router.reconfigure(new_app)
        assert router.passthrough

    async def test_passthrough_completion_delegates_to_httpx(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://fake:1234/v1",
                    "api_key": "test-key",
                },
            }
        ]
        router = LLMRouter(app)
        mock_resp = types.SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "hello"}}]},
        )
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        router._http_client = mock_client

        result = await router.acompletion(
            model="any-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result["choices"][0]["message"]["content"] == "hello"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "any-model" in str(call_kwargs)
        assert (
            call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-key"
        )

    async def test_passthrough_stream_delegates_to_httpx(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://fake:1234/v1",
                    "api_key": "test-key",
                },
            }
        ]
        router = LLMRouter(app)
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client = AsyncMock()
        mock_client.build_request.return_value = "fake-request"
        mock_client.send.return_value = mock_resp
        router._http_client = mock_client

        resp = await router.passthrough_completion_stream(
            {"model": "test", "messages": [], "stream": True}
        )
        assert resp is mock_resp
        mock_client.send.assert_called_once()

    async def test_reconfigure_closes_old_client(self):
        """Reconfiguring from passthrough to router closes the httpx client."""
        import asyncio

        app = _app()
        app.state.settings.llm_models = [
            {"model_name": "*", "litellm_params": {"api_base": "http://x:1"}}
        ]
        router = LLMRouter(app)
        assert router.passthrough
        old_client = AsyncMock()
        router._http_client = old_client

        new_app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router.reconfigure(new_app)
        assert not router.passthrough
        assert router._http_client is None
        # Let the scheduled aclose task run.
        await asyncio.sleep(0)
        old_client.aclose.assert_called_once()

    async def test_reconfigure_to_empty_closes_old_client(self):
        """Reconfiguring from passthrough to no models closes the client."""
        import asyncio

        app = _app()
        app.state.settings.llm_models = [
            {"model_name": "*", "litellm_params": {"api_base": "http://x:1"}}
        ]
        router = LLMRouter(app)
        assert router.passthrough
        old_client = AsyncMock()
        router._http_client = old_client

        router.reconfigure(_app())
        assert not router.passthrough
        assert not router.active
        assert router._http_client is None
        await asyncio.sleep(0)
        old_client.aclose.assert_called_once()

    async def test_list_upstream_models_passthrough(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://fake:1234/v1",
                },
            }
        ]
        router = LLMRouter(app)
        mock_resp = types.SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "data": [
                    {"id": "model-a", "object": "model"},
                    {"id": "model-b", "object": "model"},
                ]
            },
        )
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        router._http_client = mock_client

        models = await router.list_upstream_models()
        assert len(models) == 2
        assert models[0]["id"] == "model-a"

    async def test_list_upstream_models_passthrough_with_key(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://fake:1234/v1",
                    "api_key": "secret",
                },
            }
        ]
        router = LLMRouter(app)
        mock_resp = types.SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"id": "m1", "object": "model"}]},
        )
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        router._http_client = mock_client

        models = await router.list_upstream_models()
        assert len(models) == 1
        headers = mock_client.get.call_args.kwargs.get("headers", {})
        assert headers["Authorization"] == "Bearer secret"

    async def test_list_upstream_models_passthrough_error(self):
        app = _app()
        app.state.settings.llm_models = [
            {
                "model_name": "*",
                "litellm_params": {
                    "api_base": "http://fake:1234/v1",
                },
            }
        ]
        router = LLMRouter(app)
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")
        router._http_client = mock_client

        models = await router.list_upstream_models()
        assert models == []

    async def test_list_upstream_models_router_mode(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        router = LLMRouter(app)
        models = await router.list_upstream_models()
        assert len(models) == 1
        assert models[0]["id"] == "gpt-4o"


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


class TestParseModelEntryBranchGaps2834:
    def test_entry_without_api_base_omits_it(self):
        # A bare provider/model with no api_base and no provider default:
        # the params dict carries no api_base key.
        from klangk.llm_router import parse_model_entry

        entry = parse_model_entry("my-gateway/model-x")
        params = entry["litellm_params"]
        assert params["model"] == "my-gateway/model-x"
        assert "api_base" not in params


class TestConfigureOutsideEventLoop2910:
    def test_client_close_without_loop_is_swallowed(self):
        """Configuring from a sync context (no running loop, e.g. early
        boot or atexit): the get_event_loop RuntimeError arm is a no-op."""
        router = LLMRouter(_app())
        router._http_client = AsyncMock()
        settings = make_settings(
            {"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"}
        )
        router._configure_from_settings(settings)
        assert router._http_client is None


class TestAcloseTaskRefs2928:
    """The replaced client's aclose runs as a strongly-referenced
    fire-and-forget task (#2928): held while pending, discarded on
    completion, failures logged, cancellation silent."""

    async def _drain_aclose_tasks(self) -> None:
        """Yield until the done callbacks have drained the module set.

        A task's done callbacks run one loop turn after the task itself
        completes, so a single ``sleep(0)`` races the discard.
        """
        for _ in range(100):
            if not llm_router_mod._aclose_tasks:
                return
            await asyncio.sleep(0)
        raise AssertionError("aclose tasks did not drain")

    def _passthrough_router(self):
        app = _app()
        app.state.settings.llm_models = [
            {"model_name": "*", "litellm_params": {"api_base": "http://x:1"}}
        ]
        return LLMRouter(app)

    def _reconfigure_to_router_mode(self, router, client):
        router._http_client = client
        router.reconfigure(
            _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        )
        assert router._http_client is None

    async def test_aclose_task_holds_strong_reference(self):
        """While the close is pending the task lives in the module set
        (the only strong reference); after completion it is discarded."""
        router = self._passthrough_router()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_aclose():
            started.set()
            await release.wait()

        client = AsyncMock()
        client.aclose.side_effect = slow_aclose
        self._reconfigure_to_router_mode(router, client)

        await started.wait()
        assert llm_router_mod._aclose_tasks
        release.set()
        await self._drain_aclose_tasks()
        client.aclose.assert_called_once()

    async def test_aclose_failure_is_logged_not_lost(self, caplog):
        router = self._passthrough_router()
        client = AsyncMock()
        client.aclose.side_effect = RuntimeError("boom")
        self._reconfigure_to_router_mode(router, client)

        await self._drain_aclose_tasks()
        errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "client close failed" in r.getMessage()
        ]
        assert errors, "expected the aclose failure to be logged"

    async def test_cancelled_aclose_task_is_silent(self, caplog):
        """A cancelled close logs nothing and still drains the set."""
        caplog.set_level(logging.DEBUG)
        router = self._passthrough_router()
        started = asyncio.Event()

        async def hanging_aclose():
            started.set()
            await asyncio.Event().wait()

        client = AsyncMock()
        client.aclose.side_effect = hanging_aclose
        self._reconfigure_to_router_mode(router, client)
        await started.wait()

        (task,) = tuple(llm_router_mod._aclose_tasks)
        task.cancel()
        await self._drain_aclose_tasks()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
