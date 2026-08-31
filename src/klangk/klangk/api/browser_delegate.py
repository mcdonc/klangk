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

from .common import get_app_dep
from .common import (
    require_workspace_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_delegate_enabled(app) -> None:
    """Refuse the request when the deploy disabled the bridge (#2710).

    ``KLANGKD_BROWSER_DELEGATE_ENABLED=false`` (read live off settings, so
    a SIGHUP reload applies immediately) makes both delegate endpoints
    return 403 before any browser/session resolution happens — no bridge
    request is ever relayed to a browser tab.
    """
    if not app.state.settings.browser_delegate_enabled:
        raise HTTPException(
            status_code=403, detail="Browser delegate is disabled"
        )


class BrowserDelegateRequest(BaseModel):
    model_config = {"extra": "allow"}
    action: str
    browser_id: str


def _resolve_bridge_target(
    body: BrowserDelegateRequest,
    container_registry,
    sockets,
    token_workspace_id: str,
):
    """Resolve a browser ID to (session, target_sock, payload).

    The browser must belong to the caller's workspace: a token for
    workspace A may never relay through a browser registered against
    workspace B (#1715). Raises HTTPException (403/502) if the browser ID
    is unknown, bound to another workspace, the workspace has no
    session, or the target browser is not subscribed.
    """
    resolved = container_registry.resolve_browser(body.browser_id)
    if resolved is None:
        raise HTTPException(status_code=403, detail="Unknown browser ID")
    workspace_id, target_sock = resolved

    if workspace_id != token_workspace_id:
        # Same detail as the unknown-ID branch: a mismatched (i.e.
        # cross-workspace) browser_id must be indistinguishable from a
        # bogus one, so the relay is not a liveness oracle for other
        # workspaces' tabs (#1715).
        raise HTTPException(status_code=403, detail="Unknown browser ID")

    session = sockets.get_session(token_workspace_id)
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
    app=Depends(get_app_dep),
):
    """Bridge endpoint for container processes to delegate actions to the browser.

    The container reads the current browser ID via ``klangk-browser-id``
    and includes it in the POST.  The backend resolves the ID to the
    specific browser tab's WebSocket and relays the request.
    """
    _require_delegate_enabled(app)
    session, target_sock, payload = _resolve_bridge_target(
        body, app.state.container_registry, app.state.sockets, workspace_id
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
    app=Depends(get_app_dep),
):
    """Streaming bridge: relay browser output chunks back as NDJSON.

    For long-running actions (RAG + LLM), the browser pushes incremental
    browser_chunk messages and a terminal browser_response.  Each is streamed
    to the caller immediately, so there is no single bounded round-trip — the
    only limit is the per-chunk idle timeout, which resolves per-workspace
    (#864): the workspace's ``settings.bridge_timeout`` override > the
    ``KLANGKD_BRIDGE_TIMEOUT_SECONDS`` deploy default > 30s.
    """
    _require_delegate_enabled(app)
    session, target_sock, payload = _resolve_bridge_target(
        body, app.state.container_registry, app.state.sockets, workspace_id
    )
    # Fetch the workspace so its settings.bridge_timeout override can apply.
    # One DB lookup per stream request — these are not high-frequency
    # (one per browser-delegated long-running action from the container).
    workspace = await app.state.model.workspaces.get_workspace_by_id(
        workspace_id
    )
    return StreamingResponse(
        session.dispatch_browser_request_stream_to(
            target_sock,
            payload,
            app.state.util.bridge_idle_timeout_for(workspace),
        ),
        media_type="application/x-ndjson",
    )
