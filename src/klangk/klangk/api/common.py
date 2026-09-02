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
from ..settings import parse_bool_setting

logger = logging.getLogger(__name__)

# The full permission vocabulary: every string an ACE may carry and every
# permission the permission layer can be asked about. ``api/__init__.py``
# re-exports it (``klangk.api.ALL_PERMISSIONS``), and the roles endpoint
# uses it to expand each role group's effective grants (#2986). Kept here
# — a module no route submodule imports from — so workspaces.py can use it
# without a circular import through ``api/__init__``.
ALL_PERMISSIONS = [
    "view",
    "monitor-workspace",
    "create-workspace",
    "duplicate-workspace",
    "edit-workspace",
    "delete-workspace",
    "start-workspace",
    "stop-workspace",
    "restart-workspace",
    "transfer-workspace",
    "join-workspace",
    "terminal",
    "egress-consent",
    "code-in-isolation",
    "exec-and-sync",
    "spectate-on-shared-terminals",
    "code-in-shared-terminals",
    "share-terminals",
    "files-view",
    "files-download",
    "files-write",
    "export-workspace",
    "share-workspace",
    "share-advanced",
    "manage-users",
    "manage-invitations",
    "manage-groups",
    "manage-server-schedule",
    "manage-acls",
    "manage-events",
    "manage-volumes",
    "view-volumes",
    "view-images",
    "search-users",
    "*",
]


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


async def workspace_collection_resource(
    request: Request,
    user: dict,  # noqa: ARG001
) -> str:
    """Resource function for collection-level workspace checks (#2569)."""
    return "/workspaces"


async def workspace_resource(request: Request, user: dict) -> str:
    """Resource function for workspace-level permission checks."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


def workstation(request: Request) -> tuple[str | None, str | None]:
    """The ``(source_ip, user_agent)`` a session is established from (#2586).

    The IP is the effective client address, resolved proxy-trust-aware
    (``X-Real-IP``/``X-Forwarded-For`` honored only behind a trusted
    proxy), so a workstation identity cannot be spoofed by a direct
    caller. Both values may be ``None`` (unknown) — the audit layer
    treats unknown as never-different, never same.
    """
    ip = request.app.state.util.effective_client_ip(
        request.headers, request.client.host if request.client else None
    )
    return ip, request.headers.get("user-agent") or None


async def require_workspace_token(request: Request) -> str:
    """FastAPI dependency: validate workspace JWT from Authorization header.

    Returns the workspace_id. Raises 401 if missing, expired, or invalid.
    This duplicates the proxy auth_request check as defense-in-depth.
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


def get_app_dep(request: Request):
    """Per-request bridge to the FastAPI ``app`` (no global read).

    Request handlers obtain the app via
    ``app = Depends(get_app_dep)`` instead of
    reaching for module-level globals (#1426, #1475).
    """
    return request.app


def autostart_allowed(app) -> bool:
    """Whether per-workspace auto-start is permitted (KLANGKD_ALLOW_AUTOSTART).

    Read off the frozen ``app.state.settings`` rather than re-resolving the
    env at call time (#1516). Parsed via the shared :func:`parse_bool_setting`
    so this read and the boot-time gate in
    ``workspaces.auto_start_workspaces`` agree (#2796).
    """
    return parse_bool_setting(app.state.settings.allow_autostart)


class WorkspaceAclEntry(BaseModel):
    action: int  # 0=deny, 1=allow
    principal_type: int  # 0=system, 1=user, 2=group
    permission: str
    user_id: str | None = None
    group_id: str | None = None
    system_principal: int | None = None


def serialize_acl_entries(entries: list[WorkspaceAclEntry]) -> list[dict]:
    """Map an ACE request list to the model's row-dict shape, renumbering
    positions in submission order (shared by the admin resource-level and
    per-workspace ACL replace endpoints)."""
    return [
        {
            "position": i,
            "action": e.action,
            "principal_type": e.principal_type,
            "permission": e.permission,
            "user_id": e.user_id,
            "group_id": e.group_id,
            "system_principal": e.system_principal,
        }
        for i, e in enumerate(entries)
    ]
