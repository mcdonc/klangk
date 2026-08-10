"""Receive endpoint for sidecar egress-consent events (#2242).

The sidecar POSTs each observed blocked destination here, authenticating with
the workspace's own JWT (the same one the workspace container holds) — Caddy's
egress-port ``forward_auth`` validates it, and this handler re-decodes it for
the workspace id (defense-in-depth, like every workspace-token route). There is
no bespoke credential; the workspace id comes straight from the token,
so the monitor needs no tag-to-id resolution.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRouter

from ._common import require_workspace_token

router = APIRouter()


@router.post("/internal/egress-consent/events")
async def post_egress_event(
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
) -> dict:
    """Receive an observed blocked destination from the sidecar."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")
    dst = body.get("dst")
    if not isinstance(dst, str):
        raise HTTPException(status_code=400, detail="dst string required")
    dport = body.get("dport")
    if dport is not None and not isinstance(dport, int):
        raise HTTPException(status_code=400, detail="dport must be an int")
    request.app.state.consent_monitor.submit(workspace_id, dst, dport)
    return {"ok": True}
