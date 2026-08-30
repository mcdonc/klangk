"""Unit tests for /llm-proxy/ FastAPI endpoints (#2072)."""

import types
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk.api.llm_proxy import router
from klangk.llm_router import LLMRouter
from _helpers import make_settings

# A token the fake auth layer accepts (the endpoints only validate its
# shape via decode_workspace_token — #2890's main-listener gate).
_WS_TOKEN = "ws-test-token"


class _FakeAuth:
    """Just enough auth state for require_workspace_token (#2890)."""

    @staticmethod
    def decode_workspace_token(token: str):
        return "ws-test" if token == _WS_TOKEN else None


def _app(extra_env=None):
    """Build a minimal FastAPI app with the llm-proxy router."""
    env = dict(extra_env or {})
    settings = make_settings(env=env)
    mock_app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app = FastAPI()
    app.state.llm_router = LLMRouter(mock_app_state)
    app.state.auth = _FakeAuth()
    app.include_router(router)
    return app


def _client(app, token: str | None = _WS_TOKEN):
    """A client whose requests carry the workspace JWT by default.

    Pass ``token=None`` (or a wrong token) to exercise the 401 paths.
    """
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"} if token else None,
    )


class TestListModels:
    async def test_returns_models(self):
        app = _app(
            {
                "KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx,ollama/llama3:http://x:11434:"
            }
        )
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "gpt-4o" in ids
        assert "llama3" in ids

    async def test_empty_when_no_models(self):
        app = _app()
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    async def test_model_shape(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models")
        model = resp.json()["data"][0]
        assert model["object"] == "model"
        assert model["owned_by"] == "klangk"
        assert model["id"] == "gpt-4o"


class TestWorkspaceTokenGate:
    """Both endpoints require a workspace JWT (#2890).

    The router is mounted on the main listener too, where the
    browser-site catch-all proxies /llm-proxy/* with no auth
    subrequest — this gate is what closes anonymous access there.
    """

    async def test_models_401_without_token(self):
        app = _app(
            {
                "KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx,ollama/llama3:http://x:11434:"
            }
        )
        async with _client(app, token=None) as client:
            resp = await client.get("/llm-proxy/models")
        assert resp.status_code == 401
        assert "workspace token" in resp.json()["detail"].lower()

    async def test_models_401_with_invalid_token(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        async with _client(app, token="wrong-token") as client:
            resp = await client.get("/llm-proxy/models")
        assert resp.status_code == 401

    async def test_chat_completions_401_without_token(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        async with _client(app, token=None) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 401

    async def test_chat_completions_401_with_invalid_token(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        async with _client(app, token="wrong-token") as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 401


class TestChatCompletions:
    async def test_503_when_not_active(self):
        app = _app()
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 503

    async def test_delegates_to_router(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router
        mock_resp = types.SimpleNamespace(
            model_dump=lambda: {
                "choices": [{"message": {"content": "hello"}}],
            }
        )
        lr.acompletion = AsyncMock(return_value=mock_resp)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "hello"

    async def test_delegates_to_router_async_model_dump(self):
        """model_dump() that returns a coroutine is awaited."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router

        async def async_model_dump():
            return {"choices": [{"message": {"content": "async"}}]}

        mock_resp = types.SimpleNamespace(model_dump=async_model_dump)
        lr.acompletion = AsyncMock(return_value=mock_resp)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "async"

    async def test_delegates_plain_dict_response(self):
        """A plain dict response (no model_dump) is returned as-is."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router
        lr.acompletion = AsyncMock(
            return_value={"choices": [{"message": {"content": "dict"}}]}
        )
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "dict"

    async def test_delegates_iterable_response(self):
        """A non-dict iterable response (e.g. NamedTuple) is cast via dict()."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router
        # Return something dict()-able but not a dict and without model_dump.
        lr.acompletion = AsyncMock(
            return_value=[("choices", [{"message": {"content": "iter"}}])]
        )
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "iter"

    async def test_router_streaming(self):
        """stream=true in router mode returns an SSE stream."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router

        async def _gen():
            yield types.SimpleNamespace(
                model_dump=lambda: {"choices": [{"delta": {"content": "hi"}}]}
            )
            yield types.SimpleNamespace(
                model_dump=lambda: {
                    "choices": [{"delta": {"content": " there"}}]
                }
            )

        lr.acompletion = AsyncMock(return_value=_gen())
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        body = resp.text
        assert "data:" in body
        assert "[DONE]" in body
        assert "hi" in body

    async def test_router_streaming_async_model_dump(self):
        """Streaming chunks with async model_dump are awaited."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router

        async def async_dump():
            return {"choices": [{"delta": {"content": "async"}}]}

        async def _gen():
            yield types.SimpleNamespace(model_dump=async_dump)

        lr.acompletion = AsyncMock(return_value=_gen())
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        assert "async" in resp.text

    async def test_router_streaming_dict_chunks(self):
        """Streaming with plain dict chunks works."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router

        async def _gen():
            yield {"choices": [{"delta": {"content": "ok"}}]}

        lr.acompletion = AsyncMock(return_value=_gen())
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        assert "ok" in resp.text

    async def test_router_streaming_str_chunks(self):
        """Streaming with string chunks works."""
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router

        async def _gen():
            yield "raw-string-chunk"

        lr.acompletion = AsyncMock(return_value=_gen())
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        assert "raw-string-chunk" in resp.text

    async def test_502_on_upstream_error(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        lr = app.state.llm_router
        lr.acompletion = AsyncMock(side_effect=RuntimeError("upstream down"))
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 502
        assert "upstream request failed" in resp.json()["error"]

    async def test_passthrough_streaming(self):
        """stream=true in passthrough mode returns an SSE stream."""
        app = _app()
        mock_app_state = types.SimpleNamespace(
            state=types.SimpleNamespace(
                settings=make_settings({}),
            )
        )
        from klangk.llm_router import LLMRouter

        mock_app_state.state.settings.llm_models = [
            {"model_name": "*", "litellm_params": {"api_base": "http://x:1"}}
        ]
        lr = LLMRouter(mock_app_state)
        app.state.llm_router = lr

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None

        async def fake_lines():
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        mock_resp.aiter_lines = fake_lines
        mock_resp.aclose = AsyncMock()

        mock_client = AsyncMock()
        mock_client.build_request.return_value = "fake-req"
        mock_client.send.return_value = mock_resp
        lr._http_client = mock_client

        app.include_router(router)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        body = resp.text
        assert "data:" in body
        assert "[DONE]" in body
        mock_resp.aclose.assert_called_once()
