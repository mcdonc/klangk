"""Unit tests for /llm-proxy/ FastAPI endpoints (#2072)."""

import types
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk.api.llm_proxy import router
from klangk.llm_router import LLMRouter
from _helpers import make_settings


_DATA_DIR = {}


def _app(extra_env=None):
    """Build a minimal FastAPI app with the llm-proxy router.

    #2946: the proxy routes are permission-gated (use-llm-proxy on
    /llm-proxy), so the app carries the auth router, a per-call DB, and
    the seeded Allow-Authenticated row; tests authenticate via
    ``_headers(app)``.
    """
    import tempfile

    # NB: api/__init__ rebinds the `auth` name to the logic module
    # (see its own comment); import the route module by its real path.
    from importlib import import_module

    auth_routes = import_module("klangk.api.auth")
    from _helpers import wire_db_and_model

    tmp = tempfile.mkdtemp(prefix="llm-proxy-test-")
    env = dict(extra_env or {})
    env["KLANGKD_DATA_DIR"] = tmp
    env.setdefault("KLANGKD_AUTH_MODES", "password")
    settings = make_settings(env=env)
    mock_app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app = FastAPI()
    app.state.settings = settings
    from klangk.util import Util

    app.state.util = Util(app)
    from klangk.auth import Auth

    app.state.auth = Auth(app)
    app.state.llm_router = LLMRouter(mock_app_state)
    from klangk import oidc as oidc_mod

    app.state.oidc = oidc_mod.OIDC(app)
    wire_db_and_model(app)
    app.include_router(router)
    from klangk.util import API_PREFIX

    app.include_router(auth_routes.router, prefix=API_PREFIX)
    return app


async def _init(app):
    """init_db + test user + the /llm-proxy seed pair."""
    from klangk.model import ACTION_ALLOW, ACTION_DENY
    from klangk.model.acl import (
        PRINCIPAL_SYSTEM,
        SYSTEM_AUTHENTICATED,
        SYSTEM_EVERYONE,
    )
    from klangk.auth import hash_password

    await app.state.model.init_db()
    await app.state.model.users.create_user(
        "llm@example.com", hash_password("testpass"), verified=True
    )
    acl = app.state.model.acl
    await acl.add_acl_entry(
        "/llm-proxy",
        0,
        ACTION_ALLOW,
        "use-llm-proxy",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await acl.add_acl_entry(
        "/llm-proxy",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )


async def _headers(app):
    """Login as the seeded user; returns the auth header."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"identifier": "llm@example.com", "password": "testpass"},
        )
        token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "gpt-4o" in ids
        assert "llama3" in ids

    async def test_empty_when_no_models(self):
        app = _app()
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    async def test_model_shape(self):
        app = _app({"KLANGKD_LLM_MODELS": "openai/gpt-4o::sk-xxx"})
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models", headers=headers)
        model = resp.json()["data"][0]
        assert model["object"] == "model"
        assert model["owned_by"] == "klangk"
        assert model["id"] == "gpt-4o"


class TestChatCompletions:
    async def test_503_when_not_active(self):
        app = _app()
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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
        await _init(app)
        headers = await _headers(app)
        async with _client(app) as client:
            resp = await client.post(
                "/llm-proxy/chat/completions",
                headers=headers,
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


class TestWorkspaceTokenPath:
    """#2946: the container→host path presents a workspace JWT; the
    gate checks use-llm-proxy against the workspace OWNER's
    principals, so a user-level deny cuts their workspaces too."""

    async def _setup(self, app):
        """User + workspace owned by them; returns the workspace id."""
        from klangk.auth import hash_password

        await _init(app)
        users = app.state.model.users
        owner = await users.create_user(
            "owner@example.com", hash_password("testpass"), verified=True
        )
        from klangk.workspaces import Workspaces

        from unittest.mock import AsyncMock, MagicMock

        registry = MagicMock()
        registry.allocate_ports = AsyncMock(return_value=[8000, 8001])
        registry.prune_workspace_registry_entries = MagicMock()
        app.state.container_registry = registry
        app.state.workspaces = Workspaces(app)
        ws = await app.state.workspaces.create_workspace(owner["id"], "llm-ws")
        return owner, ws

    async def test_workspace_token_passes(self):
        app = _app()
        owner, ws = await self._setup(app)
        token = app.state.auth.create_workspace_token(ws["id"])
        async with _client(app) as client:
            resp = await client.get(
                "/llm-proxy/models",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200

    async def test_denied_owner_denies_workspace_token(self):
        app = _app()
        owner, ws = await self._setup(app)
        # Deny everyone on /llm-proxy ahead of the seeded Allow.
        from klangk.model import ACTION_DENY
        from klangk.model.acl import PRINCIPAL_USER

        await app.state.model.acl.add_acl_entry(
            "/llm-proxy",
            -1,
            ACTION_DENY,
            "*",
            PRINCIPAL_USER,
            user_id=owner["id"],
        )
        token = app.state.auth.create_workspace_token(ws["id"])
        async with _client(app) as client:
            resp = await client.get(
                "/llm-proxy/models",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 403

    async def test_garbage_token_rejected(self):
        app = _app()
        async with _client(app) as client:
            resp = await client.get(
                "/llm-proxy/models",
                headers={"Authorization": "Bearer not-a-token"},
            )
        assert resp.status_code == 401


class TestGateBranches:
    """The user-JWT probe's degenerate outcomes fall through to the
    workspace-token path (and 401 when neither validates)."""

    async def _authed_probe(self, app, token):
        async with _client(app) as client:
            return await client.get(
                "/llm-proxy/models",
                headers={"Authorization": f"Bearer {token}"},
            )

    async def test_user_jwt_for_unknown_user_falls_through(self):
        from jose import jwt

        app = _app()
        await _init(app)
        token = jwt.encode(
            {"sub": "no-such-user", "jti": "x"},
            app.state.settings.jwt_secret,
            algorithm="HS256",
        )
        resp = await self._authed_probe(app, token)
        # Not a workspace token either -> 401 from the workspace path.
        assert resp.status_code == 401

    async def test_user_jwt_without_sub_jti_falls_through(self):
        from jose import jwt

        app = _app()
        await _init(app)
        token = jwt.encode(
            {"unrelated": "claims"},
            app.state.settings.jwt_secret,
            algorithm="HS256",
        )
        resp = await self._authed_probe(app, token)
        assert resp.status_code == 401

    async def test_no_token_401(self):
        app = _app()
        await _init(app)
        async with _client(app) as client:
            resp = await client.get("/llm-proxy/models")
        assert resp.status_code == 401
