"""LLM proxy endpoints backed by the in-process litellm Router (#2072).

Handles ``/llm-proxy/`` requests that were previously forwarded by the
reverse proxy to an external LLM base URL or a LiteLLM sidecar container.
Now served in-process by :class:`~klangk.llm_router.LLMRouter`.
"""

import inspect
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .common import require_workspace_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-proxy", tags=["llm-proxy"])


@router.get("/models")
async def list_models(
    request: Request,
    _workspace_id: str = Depends(require_workspace_token),
):
    """Return the list of models the LLM router knows about.

    Requires a workspace JWT (``Authorization: Bearer …``, #2890): the
    legitimate caller is an in-workspace client reaching the backend
    through the egress site, whose ``forward_auth`` already validates
    the same token — but the router is also mounted on the main
    listener, whose browser-site catch-all proxies ``/llm-proxy/*``
    with no auth subrequest. Without this check that path exposed
    model enumeration and unauthenticated completions whenever an LLM
    router is configured.

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


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    _workspace_id: str = Depends(require_workspace_token),
):
    """Proxy a chat completion request to the litellm Router.

    Requires a workspace JWT like ``GET /models`` (#2890) — see there
    for the main-listener exposure this closes.

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
        is_stream = body.get("stream", False)

        # Passthrough streaming: forward the SSE stream from the upstream.
        if is_stream and llm_router.passthrough:
            resp = await llm_router.passthrough_completion_stream(body)

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

        response = await llm_router.acompletion(**body)

        # litellm returns an async generator when stream=True.
        if hasattr(response, "__aiter__"):

            async def stream_litellm():
                async for chunk in response:
                    if hasattr(chunk, "model_dump"):
                        data = chunk.model_dump()
                        if inspect.isawaitable(data):
                            data = await data
                    elif isinstance(chunk, dict):
                        data = chunk
                    else:
                        data = str(chunk)
                    yield f"data: {json.dumps(data)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                stream_litellm(),
                media_type="text/event-stream",
            )

        # Non-streaming: convert to a plain dict for JSONResponse.
        if hasattr(response, "model_dump"):
            data = response.model_dump()
            if inspect.isawaitable(data):
                data = await data
        elif isinstance(response, dict):
            data = response
        else:
            data = dict(response)
        return JSONResponse(content=data)
    except Exception:
        logger.exception("LLM completion failed")
        return JSONResponse(
            status_code=502,
            content={"error": "LLM upstream request failed"},
        )
