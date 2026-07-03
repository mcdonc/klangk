"""Admin routes: user admin, invitation admin, group admin + the user-accessible /groups endpoints, and the admin ACL tree/resource endpoints."""

import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from pydantic import BaseModel

from .. import (
    acl,
    auth,
    container,
    emailsvc,
    model,
    wshandler,
    workspaces,
)
from ..model import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
)
from ..util import (
    derive_hosting_info,
)
from ._common import (
    WorkspaceAclEntry,
    _admin_resource,
    _send_email,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SendInviteRequest(BaseModel):
    email: str


@router.post("/admin/invitations")
async def send_invitation(
    req: SendInviteRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Send an invitation email (admin only)."""
    if not auth.invitations_enabled():
        raise HTTPException(status_code=403, detail="Invitations are disabled")

    auth.validate_email(req.email)

    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="A user with this email already exists"
        )

    pending = await model.get_pending_invitation_by_email(req.email)
    if pending is not None:
        raise HTTPException(
            status_code=400,
            detail="A pending invitation already exists for this email",
        )

    invitation = await model.create_invitation(req.email, admin["id"])
    token = auth.create_invitation_token(invitation["id"], req.email)

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await _send_email(
        emailsvc.send_invitation_email(req.email, invite_url, admin["email"]),
        req.email,
        "invitation email",
    )

    return {
        "id": invitation["id"],
        "email": invitation["email"],
        "status": invitation["status"],
    }


@router.get("/admin/invitations")
async def list_invitations(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """List invitations (admin only), server-side paginated/sorted/filtered.

    Returns a paged envelope ``{invitations, page, page_size, total,
    pending_count}`` supporting forwards/backwards paging. ``sort`` is one
    of ``email`` | ``invited_by`` | ``created``, ``order`` is ``asc`` |
    ``desc``, and ``q`` is a substring filter on the invitee email.
    """
    return await model.list_invitations(
        page=page, page_size=page_size, sort=sort, order=order, q=q
    )


@router.delete("/admin/invitations/{invitation_id}")
async def revoke_invitation(
    invitation_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Revoke a pending invitation (admin only)."""
    revoked = await model.revoke_invitation(invitation_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )
    return {"status": "revoked"}


@router.post("/admin/invitations/{invitation_id}/resend")
async def resend_invitation(
    invitation_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Resend an invitation email (admin only)."""
    invitation = await model.get_invitation(invitation_id)
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )

    token = auth.create_invitation_token(invitation["id"], invitation["email"])
    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await _send_email(
        emailsvc.send_invitation_email(
            invitation["email"], invite_url, admin["email"]
        ),
        invitation["email"],
        "invitation email",
    )

    return {"status": "resent"}


@router.get("/admin/users")
async def list_users(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.list_users(
        page=page, page_size=page_size, sort=sort, order=order, q=q
    )


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str | None = None
    send_verification_email: bool = False


@router.post("/admin/users")
async def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Create a user (admin only).

    By default creates a verified user with the given password.  When
    ``send_verification_email`` is true, the password field is ignored
    and a verification email is sent so the user can set their own
    password.
    """
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    if req.send_verification_email:
        user_id = str(uuid.uuid4())
        # Use a random password hash — the user will set their own
        # password via the verification link.
        password_hash = auth.hash_password(uuid.uuid4().hex[:24])

        hostname, proto, base_path = derive_hosting_info(
            request.headers, request.client.host if request.client else None
        )
        verification_token = auth.create_verification_token(user_id)
        verification_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/verify?token={verification_token}"
        )

        async with model.transaction() as db:
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified)"
                " VALUES (?, ?, ?, 0)",
                (user_id, req.email, password_hash),
            )
            await _send_email(
                emailsvc.send_verification_email(req.email, verification_url),
                req.email,
                "verification email",
            )

        return {
            "id": user_id,
            "email": req.email,
            "status": "pending_verification",
        }

    if not req.password:
        raise HTTPException(
            status_code=400,
            detail="Password is required when not sending verification email",
        )
    auth.validate_password_length(req.password)
    password_hash = auth.hash_password(req.password)
    user = await model.create_user(req.email, password_hash, verified=True)
    return {"id": user["id"], "email": user["email"], "status": "created"}


@router.get("/admin/users/{user_id}/workspaces")
async def list_user_workspaces(
    user_id: str,
    limit: int | None = Query(None, ge=1, le=200),
    offset: int | None = Query(None, ge=0),
    admin: dict = Depends(acl.has_permission("admin")),
):
    """List workspaces owned by a user (admin only).

    Used by the admin UI to show what a delete-user will destroy (#1224).
    Returns the standard pagination envelope
    ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
    """
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await workspaces.list_workspaces(
        user_id, limit=limit or 100, offset=offset or 0
    )


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str, admin: dict = Depends(acl.has_permission("admin"))
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Stop all containers for this user before deleting
    await container.registry.stop_user_containers(user_id)
    # Archive workspace data before deletion
    await workspaces.archive_user_data(user_id, user["email"])
    deleted = await model.delete_user(user_id)
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted"}


class UpdateUserRequest(auth.BaseModel):
    email: str | None = None
    password: str | None = None
    handle: str | None = None


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if req.email is not None:
        await model.update_email(user_id, req.email)
    if req.password is not None:
        auth.validate_password_length(req.password)
        password_hash = auth.hash_password(req.password)
        await model.update_password(user_id, password_hash)
    if req.handle is not None:
        try:
            await model.set_user_handle(user_id, req.handle)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await wshandler.refresh_user_handle(user_id, req.handle)
    return {"status": "updated"}


@router.post("/admin/users/{user_id}/unlockout")
async def unlock_user(
    user_id: str, admin: dict = Depends(acl.has_permission("admin"))
):
    """Reset a user's login lockout so they can log in immediately."""
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.clear_login_attempts(user["email"])
    return {"status": "unlocked"}


# --- Group management endpoints ---


class CreateGroupRequest(BaseModel):
    name: str
    description: str | None = None


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class AddGroupMemberRequest(BaseModel):
    user_id: str


# --- User-accessible group endpoints (ACL-gated per group) ---


async def _group_resource(request: Request, user: dict) -> str:  # noqa: ARG001
    """Resource function for group-level permission checks."""
    group_id = request.path_params.get("group_id")
    if group_id:
        return f"/groups/{group_id}"
    return "/groups"


@router.get("/groups")
async def user_list_groups(
    user: dict = Depends(auth.get_current_user),
):
    """List all groups (any authenticated user can see groups).

    Returns a bare list for backward compatibility; the admin endpoint
    exposes the paged envelope.
    """
    result = await model.list_groups(page_size=200)
    return result["groups"]


@router.post("/groups")
async def user_create_group(
    req: CreateGroupRequest,
    user: dict = Depends(acl.has_permission("create", _group_resource)),
):
    """Create a group. The creator gets full ACL access."""
    existing = await model.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await model.create_group(req.name, req.description)
    # Grant creator full access via ACL
    await model.add_acl_entry(
        f"/groups/{group['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return group


@router.patch("/groups/{group_id}")
async def user_update_group(
    group_id: str,
    req: UpdateGroupRequest,
    user: dict = Depends(acl.has_permission("edit", _group_resource)),
):
    """Update a group (requires edit permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    updated = await model.update_group(
        group_id, name=req.name, description=req.description
    )
    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "updated"}


@router.delete("/groups/{group_id}")
async def user_delete_group(
    group_id: str,
    user: dict = Depends(acl.has_permission("delete", _group_resource)),
):
    """Delete a group (requires delete permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    await model.delete_group(group_id)
    await model.delete_acl_entries_for_resource(f"/groups/{group_id}")
    return {"status": "deleted"}


@router.get("/groups/{group_id}/members")
async def user_list_group_members(
    group_id: str,
    user: dict = Depends(acl.has_permission("view", _group_resource)),
):
    """List group members (requires view permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return await model.get_group_members(group_id)


@router.post("/groups/{group_id}/members")
async def user_add_group_member(
    group_id: str,
    req: AddGroupMemberRequest,
    user: dict = Depends(
        acl.has_permission("manage_members", _group_resource)
    ),
):
    """Add a member (requires manage_members permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    target = await model.get_user_by_id(req.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(req.user_id, group_id)
    return {"status": "added"}


@router.delete("/groups/{group_id}/members/{user_id}")
async def user_remove_group_member(
    group_id: str,
    user_id: str,
    user: dict = Depends(
        acl.has_permission("manage_members", _group_resource)
    ),
):
    """Remove a member (requires manage_members on the group)."""
    removed = await model.remove_user_from_group(user_id, group_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    return {"status": "removed"}


# --- Admin group endpoints (admin-only, kept for backward compat) ---


@router.get("/admin/groups")
async def list_groups(
    page: int = 1,
    page_size: int = 10,
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.list_groups(
        page=page, page_size=page_size, sort=sort, order=order, q=q
    )


@router.post("/admin/groups")
async def create_group(
    req: CreateGroupRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    existing = await model.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await model.create_group(req.name, req.description)
    return group


@router.patch("/admin/groups/{group_id}")
async def update_group(
    group_id: str,
    req: UpdateGroupRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    updated = await model.update_group(
        group_id, name=req.name, description=req.description
    )
    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "updated"}


@router.delete("/admin/groups/{group_id}")
async def delete_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    await model.delete_group(group_id)
    return {"status": "deleted"}


@router.get("/admin/groups/{group_id}/members")
async def list_group_members(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return await model.get_group_members(group_id)


@router.post("/admin/groups/{group_id}/members")
async def add_group_member(
    group_id: str,
    req: AddGroupMemberRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    user = await model.get_user_by_id(req.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(req.user_id, group_id)
    return {"status": "added"}


@router.delete("/admin/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    removed = await model.remove_user_from_group(user_id, group_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    return {"status": "removed"}


# --- ACL management endpoints ---


@router.get("/admin/acl/tree")
async def get_acl_tree(
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_tree_summary()


@router.get("/admin/acl/by-principal/user/{user_id}")
async def get_acl_by_user(
    user_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_entries_by_principal_user(user_id)


@router.get("/admin/acl/by-principal/group/{group_id}")
async def get_acl_by_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_entries_by_principal_group(group_id)


@router.get("/admin/acl/resource")
async def get_resource_acl(
    resource: str,
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Get resolved ACL entries for any resource (admin only)."""
    return await model.get_acl_entries_resolved(resource)


@router.put("/admin/acl/resource")
async def replace_resource_acl(
    resource: str,
    entries: list[WorkspaceAclEntry],
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Replace ACL entries for any resource (admin only)."""
    # Validate: root ACL must keep Authenticated view access
    if resource == "/":
        has_auth_view = any(
            e.action == ACTION_ALLOW
            and e.principal_type == PRINCIPAL_SYSTEM
            and e.system_principal == SYSTEM_AUTHENTICATED
            and e.permission in ("view", "*")
            for e in entries
        )
        if not has_auth_view:
            raise HTTPException(
                status_code=400,
                detail="Root ACL must include Allow Authenticated view "
                "to prevent locking out all users",
            )

    # Validate: /admin ACL must keep admin group access
    if resource == "/admin":
        has_admin_group = any(
            e.action == ACTION_ALLOW
            and e.principal_type == PRINCIPAL_GROUP
            and e.permission in ("*", "admin")
            for e in entries
        )
        if not has_admin_group:
            raise HTTPException(
                status_code=400,
                detail="Admin ACL must include at least one Allow "
                "group entry to prevent locking out all admins",
            )

    acl_entries = [
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
    await model.replace_acl_entries(resource, acl_entries)
    return await model.get_acl_entries_resolved(resource)


STATIC_RESOURCES = [
    "/",
    "/workspaces",
    "/groups",
    "/admin",
    "/admin/users",
    "/admin/invitations",
    "/admin/groups",
]

ALL_PERMISSIONS = [
    "view",
    "create",
    "edit",
    "delete",
    "terminal",
    "code-in-isolation",
    "spectate-on-shared-terminals",
    "code-in-shared-terminals",
    "share-terminals",
    "files",
    "chat",
    "share",
    "manage_members",
    "admin",
    "manage_users",
    "manage_invitations",
    "*",
]
