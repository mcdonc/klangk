"""Unit tests for /llm-proxy/ FastAPI endpoints (#2072)."""

import types
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk.api.llm_proxy import router
from klangk.llm_router import LLMRouter
from _helpers import make_settings


def _app(extra_env=None):
    """Build a minimal FastAPI app with the llm-proxy router."""
    env = dict(extra_env or {})
    settings = make_settings(env=env)
    mock_app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app = FastAPI()
    app.state.llm_router = LLMRouter(mock_app_state)
    app.include_router(router)
    return app


def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
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
