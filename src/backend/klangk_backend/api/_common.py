"""Shared helpers, constants, and request models for the API package.

Only the bits that more than one per-domain route module needs live here
(email sending, the upload-size cap, ACL resource resolvers, the workspace
JWT dependency, and the ACL-entry model).  Helpers used by a single domain
stay in that domain's module.  This module deliberately imports **no** route
submodule, which would create a circular import through ``api/__init__``.
"""

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import auth

logger = logging.getLogger(__name__)


async def send_email(coro, recipient: str, kind: str = "email") -> None:
    """Await an email-sending coroutine, converting failures to 503."""
    try:
        await coro
    except Exception as e:
        logger.error("Failed to send %s to %s: %s", kind, recipient, e)
        raise HTTPException(
            status_code=503,
            detail=f"Unable to send {kind}. Please try again later.",
        ) from None


async def workspace_resource(request: Request, user: dict) -> str:
    """Resource function for workspace-level permission checks."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


async def admin_resource(request: Request, user: dict) -> str:  # noqa: ARG001
    """Resource function for admin operations (always checks /admin)."""
    return "/admin"


async def require_workspace_token(request: Request) -> str:
    """FastAPI dependency: validate workspace JWT from Authorization header.

    Returns the workspace_id. Raises 401 if missing, expired, or invalid.
    This duplicates the nginx auth_request check as defense-in-depth.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing workspace token")
    token = authorization[7:]
    result = request.app.state.auth.decode_workspace_token(token)
    if result is auth.Auth.WORKSPACE_TOKEN_EXPIRED:
        raise HTTPException(status_code=401, detail="Workspace token expired")
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid workspace token")
    return result


def get_app_state_dep(request: Request):
    """Per-request bridge to ``app.state`` (no global read).

    Request handlers obtain app state via
    ``app_state = Depends(get_app_state_dep)`` instead of
    reaching for module-level globals (#1426, #1475).
    """
    return request.app.state


def autostart_allowed(app_state) -> bool:
    """Whether per-workspace auto-start is permitted (KLANGK_ALLOW_AUTOSTART).

    Read off the frozen ``app_state.settings`` rather than re-resolving the
    env at call time (#1516).
    """
    return app_state.settings.allow_autostart.strip().lower() in (
        "1",
        "true",
        "yes",
    )


class WorkspaceAclEntry(BaseModel):
    action: int  # 0=deny, 1=allow
    principal_type: int  # 0=system, 1=user, 2=group
    permission: str
    user_id: str | None = None
    group_id: str | None = None
    system_principal: int | None = None
