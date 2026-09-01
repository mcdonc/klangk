"""LLM proxy endpoints backed by the in-process litellm Router (#2072).

Handles ``/llm-proxy/`` requests that were previously forwarded by the
reverse proxy to an external LLM base URL or a LiteLLM sidecar container.
Now served in-process by :class:`~klangk.llm_router.LLMRouter`.

#2946: the routes are permission-gated — ``use-llm-proxy`` on the
``/llm-proxy`` resource. Two caller classes reach them:

- Browsers/direct API clients presenting a **user JWT** (the
  ``Authorization: Bearer`` a logged-in session holds) — checked
  against that user's principals.
- Workspace containers presenting a **workspace JWT** (minted by the
  connect flow, forwarded by the egress caddy with its
  ``forward_auth`` container-source guard) — checked against the
  workspace *owner's* principals, so a deploy that denies
  ``use-llm-proxy`` to a user cuts their workspaces' proxy access
  too. Mirrors :func:`klangk.api.common.require_workspace_token`'s
  defense-in-depth pattern (the proxy already validated the token;
  we re-validate and add the permission check).
"""

import inspect
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from fastapi.responses import JSONResponse, StreamingResponse
from jose import JWTError

from ..acl import check_permission_inmemory
from .common import require_workspace_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-proxy", tags=["llm-proxy"])


async def llm_proxy_gate(request: Request) -> dict:
    """Dependency: require ``use-llm-proxy`` on ``/llm-proxy``.

    Accepts a user JWT or a workspace JWT (see the module docstring).
    Returns a principal-shaped dict (``user_id`` / ``group_ids`` /
    ``authenticated``) so the shared in-memory ACL check serves both.
    """
    app = request.app
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not bearer:
        raise HTTPException(status_code=401, detail="Not authenticated")

    principals = None
    try:
        payload = app.state.auth.decode_token(bearer)
        user_id = payload.get("sub")
        if user_id is not None and payload.get("jti") is not None:
            user = await app.state.model.users.get_user_by_id(user_id)
            if user is not None:
                principals = await app.state.acl.get_principals(user_id)
    except JWTError:
        principals = None

    if principals is None:
        # Not a valid user JWT — try the workspace token path.
        workspace_id = await require_workspace_token(request)
        workspace = await app.state.model.workspaces.get_workspace(
            workspace_id
        )
        if workspace is None:  # pragma: no cover — connect flow prevents
            raise HTTPException(status_code=401, detail="Unknown workspace")
        principals = await app.state.acl.get_principals(workspace["user_id"])

    entries = await app.state.model.acl.get_acl_entries("/llm-proxy")
    if not check_permission_inmemory(
        "/llm-proxy", principals, "use-llm-proxy", {"/llm-proxy": entries}
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principals


@router.get("/models")
async def list_models(
    request: Request,
    _principals: dict = Depends(llm_proxy_gate),
):
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


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    _principals: dict = Depends(llm_proxy_gate),
):
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
