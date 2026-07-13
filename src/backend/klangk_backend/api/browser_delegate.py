"""Browser-delegate bridge routes: relay container requests to the user's browser tab over the workspace WebSocket."""

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import BaseModel

from ._common import get_app_state_dep
from ._common import (
    require_workspace_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class BrowserDelegateRequest(BaseModel):
    model_config = {"extra": "allow"}
    action: str
    browser_id: str


def _resolve_bridge_target(
    body: BrowserDelegateRequest, container_registry, sockets
):
    """Resolve a browser ID to (session, target_sock, payload).

    Raises HTTPException (403/502) if the browser ID is unknown, the
    workspace has no session, or the target browser is not subscribed.
    """
    resolved = container_registry.resolve_browser(body.browser_id)
    if resolved is None:
        raise HTTPException(status_code=403, detail="Unknown browser ID")
    workspace_id, target_sock = resolved

    session = sockets.get_session(workspace_id)
    if not session:
        raise HTTPException(
            status_code=502,
            detail="No browser client connected to this workspace",
        )

    if target_sock not in session.browser_subscribers:
        raise HTTPException(
            status_code=502,
            detail="Browser connection not available",
        )
    return session, target_sock, body.model_dump(exclude={"browser_id"})


@router.post("/browser-delegate")
async def browser_delegate(
    body: BrowserDelegateRequest,
    workspace_id: str = Depends(require_workspace_token),
    app_state=Depends(get_app_state_dep),
):
    """Bridge endpoint for container processes to delegate actions to the browser.

    The container reads the current browser ID via ``klangk-browser-id``
    and includes it in the POST.  The backend resolves the ID to the
    specific browser tab's WebSocket and relays the request.
    """
    session, target_sock, payload = _resolve_bridge_target(
        body, app_state.container_registry, app_state.sockets
    )
    # Credential get operations may wait for user interaction (PAT dialog
    # or OAuth device flow) — allow up to 15 minutes (matching GitHub's
    # device code expiry).
    action = payload.get("action", "")
    operation = payload.get("operation", "")
    timeout = (
        900.0 if action == "git_credential" and operation == "get" else 30.0
    )
    result = await session.dispatch_browser_request_to(
        target_sock, payload, timeout=timeout
    )

    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/browser-delegate/stream")
async def browser_delegate_stream(
    body: BrowserDelegateRequest,
    workspace_id: str = Depends(require_workspace_token),
    app_state=Depends(get_app_state_dep),
):
    """Streaming bridge: relay browser output chunks back as NDJSON.

    For long-running actions (RAG + LLM), the browser pushes incremental
    browser_chunk messages and a terminal browser_response.  Each is streamed
    to the caller immediately, so there is no single bounded round-trip — the
    only limit is the per-chunk idle timeout.
    """
    session, target_sock, payload = _resolve_bridge_target(
        body, app_state.container_registry, app_state.sockets
    )
    return StreamingResponse(
        session.dispatch_browser_request_stream_to(
            target_sock, payload, app_state.util.bridge_idle_timeout()
        ),
        media_type="application/x-ndjson",
    )


# --- Container-to-chat API (workspace JWT auth) ---
