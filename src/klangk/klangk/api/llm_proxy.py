"""LLM proxy endpoints backed by the in-process litellm Router (#2072).

Handles ``/llm-proxy/`` requests that were previously forwarded by the
reverse proxy to an external LLM base URL or a LiteLLM sidecar container.
Now served in-process by :class:`~klangk.llm_router.LLMRouter`.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-proxy", tags=["llm-proxy"])


@router.get("/models")
async def list_models(request: Request):
    """Return the list of models the LLM router knows about.

    Matches the OpenAI ``GET /v1/models`` response shape so Pi's
    ``llm-proxy-models.ts`` model discovery works unchanged.
    """
    llm_router = request.app.state.llm_router
    names = llm_router.get_model_names()
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "klangk"}
            for name in names
        ],
    }


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """Proxy a chat completion request to the litellm Router.

    Accepts the standard OpenAI ``/v1/chat/completions`` request body
    and delegates to ``LLMRouter.acompletion()``.
    """
    llm_router = request.app.state.llm_router
    if not llm_router.active:
        return JSONResponse(
            status_code=503,
            content={"error": "LLM router not configured"},
        )
    body = await request.json()
    try:
        response = await llm_router.acompletion(**body)
        return (
            response.model_dump()
            if hasattr(response, "model_dump")
            else response
        )
    except Exception:
        logger.exception("LLM completion failed")
        return JSONResponse(
            status_code=502,
            content={"error": "LLM upstream request failed"},
        )
