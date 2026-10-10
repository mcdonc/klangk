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

from .. import forge_proxy as forge_proxy_mod
from . import forge_proxy as forge_proxy_routes
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


async def _forge_relay(app, workspace_id, session, target_sock, payload):
    """Apply the forge proxy's bridge policy; None leaves the plain relay.

    Proxied forges (forge_proxy.py): the container may only start a
    klangkd-issued authorization; its code is exchanged here and never
    relayed back, and nothing that could hand the container a forge
    credential (its own authorize URL, a PAT prompt, a cache read) passes.
    """
    forge = getattr(app.state, "forge_proxy", None)
    policy = forge.bridge_policy(payload) if forge else "pass"
    if policy == "refuse":
        raise HTTPException(
            status_code=403,
            detail="This forge is proxied: run `git-credential-klangk forge-auth <host>`",
        )
    if policy != "txn":
        return None

    async def dispatch(request, timeout):
        return await session.dispatch_browser_request_to(
            target_sock, request, timeout=timeout
        )

    client = forge_proxy_routes.http_client_for(app)
    try:
        return await forge.relay_authorization(
            workspace_id, payload, dispatch, client
        )
    except forge_proxy_mod.ForgeProxyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    finally:
        await client.aclose()


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
    relayed = await _forge_relay(
        app, workspace_id, session, target_sock, payload
    )
    if relayed is not None:
        return relayed
    # Credential get operations may wait for user interaction (PAT
    # dialog or OAuth device flow) — allow up to 15 minutes (matching
    # GitHub's device code expiry). The browser authorization flow
    # waits on the same human timescale: sign-in + 2FA + approval at the
    # provider (#3385).
    action = payload.get("action", "")
    operation = payload.get("operation", "")
    timeout = (
        900.0
        if action == "git_credential"
        and operation in ("get", "auth_flow_start")
        else 30.0
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
    # Proxied-forge credential flows are never streamed: an authorization
    # code or token must not reach the container through this path either.
    forge = getattr(app.state, "forge_proxy", None)
    if forge and forge.bridge_policy(payload) != "pass":
        raise HTTPException(
            status_code=403,
            detail="This forge is proxied: run `git-credential-klangk forge-auth <host>`",
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
