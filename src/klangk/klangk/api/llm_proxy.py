"""LLM proxy endpoints backed by the in-process litellm Router (#2072).

Handles ``/llm-proxy/`` requests that were previously forwarded by the
reverse proxy to an external LLM base URL or a LiteLLM sidecar container.
Now served in-process by :class:`~klangk.llm_router.LLMRouter`.

#2959: container-internal callers only. Every route requires a
**workspace JWT** — the in-container path through the egress caddy's
``forward_auth``, which forwards the original ``Authorization`` header
on to the backend. User JWTs and anonymous requests are rejected with
401: the proxy must not be usable from outside workspace containers,
even by authenticated users. Mirrors the defense-in-depth pattern of
:func:`klangk.api.common.require_workspace_token` (the egress proxy
already validated the token; the routes re-validate it).
"""

import inspect
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .common import require_workspace_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/llm-proxy",
    tags=["llm-proxy"],
    dependencies=[Depends(require_workspace_token)],
)


@router.get("/models")
async def list_models(request: Request):
    """Return the list of models the LLM router knows about.

    In passthrough mode, queries the upstream's ``/models`` endpoint
    for dynamic discovery.  In router mode, returns the configured
    model names.  Matches the OpenAI ``GET /v1/models`` response shape.
    """
    llm_router = request.app.state.llm_router
    models = await llm_router.list_upstream_models()
    return {
        "object": "list",
        "data": models,
    }


def _passthrough_stream_response(resp) -> StreamingResponse:
    """A StreamingResponse forwarding the upstream's SSE lines."""

    async def stream_passthrough():
        try:
            async for line in resp.aiter_lines():
                yield f"{line}\n"
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_passthrough(),
        media_type="text/event-stream",
    )


async def _chunk_data(chunk):
    """Convert one streamed chunk to its payload (model_dump, dict, or
    str fallback); awaits an async ``model_dump``."""
    if hasattr(chunk, "model_dump"):
        data = chunk.model_dump()
        if inspect.isawaitable(data):
            data = await data
        return data
    if isinstance(chunk, dict):
        return chunk
    return str(chunk)


async def _stream_litellm_response(response) -> StreamingResponse:
    """A StreamingResponse rendering the litellm async-generator chunks
    in the OpenAI SSE shape, terminated with [DONE]."""

    async def stream_litellm():
        async for chunk in response:
            data = await _chunk_data(chunk)
            yield f"data: {json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_litellm(),
        media_type="text/event-stream",
    )


async def _json_response(response) -> JSONResponse:
    """Convert a non-streaming litellm response to a plain dict for
    JSONResponse; awaits an async ``model_dump``."""
    if hasattr(response, "model_dump"):
        data = response.model_dump()
        if inspect.isawaitable(data):
            data = await data
    elif isinstance(response, dict):
        data = response
    else:
        data = dict(response)
    return JSONResponse(content=data)


async def _dispatch_completion(llm_router, body):
    """Route one completion body to its response flavor: passthrough
    SSE, litellm async-generator SSE, or plain JSON."""
    if body.get("stream", False) and llm_router.passthrough:
        resp = await llm_router.passthrough_completion_stream(body)
        return _passthrough_stream_response(resp)
    response = await llm_router.acompletion(**body)
    # litellm returns an async generator when stream=True.
    if hasattr(response, "__aiter__"):
        return await _stream_litellm_response(response)
    # Non-streaming: convert to a plain dict for JSONResponse.
    return await _json_response(response)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """Proxy a chat completion request to the litellm Router.

    Accepts the standard OpenAI ``/v1/chat/completions`` request body
    and delegates to ``LLMRouter.acompletion()``.  In passthrough mode
    with ``stream: true``, the upstream's SSE stream is forwarded
    directly to the client.
    """
    llm_router = request.app.state.llm_router
    if not llm_router.active:
        return JSONResponse(
            status_code=503,
            content={"error": "LLM router not configured"},
        )
    body = await request.json()
    try:
        return await _dispatch_completion(llm_router, body)
    except Exception:
        logger.exception("LLM completion failed")
        return JSONResponse(
            status_code=502,
            content={"error": "LLM upstream request failed"},
        )
