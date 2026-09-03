"""Admin routes: user admin, invitation admin, group admin + the user-accessible /groups endpoints, and the admin ACL tree/resource endpoints."""

import asyncio
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
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .. import (
    acl,
    auth,
    wshandler,
)
from ..server_schedule import resolve_fire_at
from .common import get_app_dep
from ..model import (
    ACTION_ALLOW,
    AgentPrincipalError,
    GROUP_SOURCES,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
)
from .common import (
    WorkspaceAclEntry,
    send_email,
    serialize_acl_entries,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SendInviteRequest(BaseModel):
    email: str


def _auth_view_entry(e) -> bool:
    """True when an entry grants view (or wildcard) to Authenticated."""
    return (
        e.action == ACTION_ALLOW
        and e.principal_type == PRINCIPAL_SYSTEM
        and e.system_principal == SYSTEM_AUTHENTICATED
        and e.permission in ("view", "*")
    )


def _validate_root_acl(entries, resource: str) -> None:
    """Root ACL must keep Authenticated view access."""
    if resource != "/":
        return
    has_auth_view = any(_auth_view_entry(e) for e in entries)
    if not has_auth_view:
        raise HTTPException(
            status_code=400,
            detail="Root ACL must include Allow Authenticated view "
            "to prevent locking out all users",
        )


async def create_invitation_or_race_400(app, req, admin) -> dict:
    """Create the invitation row, mapping a lost race to a 400.

    The pending pre-check in ``send_invitation`` is not atomic with the
    insert, so a concurrent send that wins the partial unique index
    (m0028) must surface as the same 400 the pre-check returns — and
    only one pending invitation survives (#3101).
    """
    try:
        return await app.state.model.invitations.create_invitation(
            req.email, admin["id"]
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=400,
            detail="A pending invitation already exists for this email",
        ) from None


@router.post("/invitations")
async def send_invitation(
    req: SendInviteRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-invitations")),
    app=Depends(get_app_dep),
):
    """Send an invitation email (admin only)."""
    if not app.state.auth.invitations_enabled():
        raise HTTPException(status_code=403, detail="Invitations are disabled")

    auth.validate_email(req.email)

    existing = await app.state.model.users.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="A user with this email already exists"
        )

    pending = (
        await app.state.model.invitations.get_pending_invitation_by_email(
            req.email
        )
    )
    if pending is not None:
        raise HTTPException(
            status_code=400,
            detail="A pending invitation already exists for this email",
        )

    invitation = await create_invitation_or_race_400(app, req, admin)
    token = app.state.auth.create_invitation_token(invitation["id"], req.email)

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await send_email(
        app.state.email.send_invitation_email(
            req.email, invite_url, admin["email"]
        ),
        req.email,
        "invitation email",
    )

    return {
        "id": invitation["id"],
        "email": invitation["email"],
        "status": invitation["status"],
    }


@router.get("/invitations")
async def list_invitations(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
    admin: dict = Depends(acl.has_permission("manage-invitations")),
    app=Depends(get_app_dep),
):
    """List invitations (admin only), server-side paginated/sorted/filtered.

    Returns a paged envelope ``{invitations, page, page_size, total,
    pending_count}`` supporting forwards/backwards paging. ``sort`` is one
    of ``email`` | ``invited_by`` | ``created``, ``order`` is ``asc`` |
    ``desc``, and ``q`` is a substring filter on the invitee email.
    """
    return await app.state.model.invitations.list_invitations(
        page=page, page_size=page_size, sort=sort, order=order, q=q
    )


@router.delete("/invitations/{invitation_id}")
async def revoke_invitation(
    invitation_id: str,
    admin: dict = Depends(acl.has_permission("manage-invitations")),
    app=Depends(get_app_dep),
):
    """Revoke a pending invitation (admin only)."""
    revoked = await app.state.model.invitations.revoke_invitation(
        invitation_id
    )
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )
    return {"status": "revoked"}


@router.post("/invitations/{invitation_id}/resend")
async def resend_invitation(
    invitation_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-invitations")),
    app=Depends(get_app_dep),
):
    """Resend an invitation email (admin only)."""
    invitation = await app.state.model.invitations.get_invitation(
        invitation_id
    )
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )

    token = app.state.auth.create_invitation_token(
        invitation["id"], invitation["email"]
    )
    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await send_email(
        app.state.email.send_invitation_email(
            invitation["email"], invite_url, admin["email"]
        ),
        invitation["email"],
        "invitation email",
    )

    return {"status": "resent"}


@router.get("/users")
async def list_users(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    return await app.state.model.users.list_users(
        page=page, page_size=page_size, sort=sort, order=order, q=q
    )


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str | None = None
    send_verification_email: bool = False


@router.post("/users")
async def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    """Create a user (admin only).

    By default creates a verified user with the given password.  When
    ``send_verification_email`` is true, the password field is ignored
    and a verification email is sent so the user can set their own
    password.
    """
    auth.validate_email(req.email)
    existing = await app.state.model.users.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    if req.send_verification_email:
        user_id = str(uuid.uuid4())
        # Use a random password hash — the user will set their own
        # password via the verification link.
        password_hash = await asyncio.to_thread(
            auth.hash_password, uuid.uuid4().hex[:24]
        )

        hostname, proto, base_path = (
            request.app.state.util.derive_hosting_info(
                request.headers,
                request.client.host if request.client else None,
            )
        )
        verification_token = app.state.auth.create_verification_token(user_id)
        verification_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/verify?token={verification_token}"
        )

        async with app.state.model.transaction() as db:
            await app.state.model.users.insert_unverified_user(
                db, user_id, req.email, password_hash
            )
            await send_email(
                app.state.email.send_verification_email(
                    req.email, verification_url
                ),
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
    app.state.auth.validate_password(req.password)
    password_hash = await asyncio.to_thread(auth.hash_password, req.password)
    user = await app.state.model.users.create_user(
        req.email, password_hash, verified=True
    )
    return {"id": user["id"], "email": user["email"], "status": "created"}


@router.get("/users/{user_id}/workspaces")
async def list_user_workspaces(
    user_id: str,
    limit: int | None = Query(None, ge=1, le=200),
    offset: int | None = Query(None, ge=0),
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    """List workspaces owned by a user (admin only).

    Used by the admin UI to show what a delete-user will destroy (#1224).
    Returns the standard pagination envelope
    ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
    """
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await app.state.workspaces.list_workspaces(
        user_id, limit=limit or 100, offset=offset or 0
    )


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Stop all containers for this user before deleting
    await app.state.container_registry.stop_user_containers(user_id)
    # Archive workspace data before deletion
    await app.state.workspaces.archive_user_data(user_id, user["email"])
    # Capture the user's workspace ids before the DB cascade removes the
    # rows, so the per-workspace registry entries can be pruned after the
    # delete (#2912).
    ws_ids = await app.state.model.workspaces.get_user_workspace_ids(user_id)
    deleted = await app.state.model.users.delete_user(user_id)
    # Prune the per-user activity-throttle stamp (#2914): placed before
    # the not-deleted race check so even a lost race (user already gone)
    # does not leave a stale entry behind.
    app.state.auth.forget_user(user_id)
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="User not found")
    # #2912: the cascade-deleted ids can never be started again -- drop
    # their registry entries (per-workspace lock + stop epoch).
    registry = app.state.container_registry
    for ws_id in ws_ids:
        registry.prune_workspace_registry_entries(ws_id)
    return {"status": "deleted"}


class UpdateUserRequest(auth.BaseModel):
    email: str | None = None
    password: str | None = None
    handle: str | None = None
    disabled: bool | None = None


async def _require_user(app, user_id: str) -> dict:
    """The user row; 404 when it does not exist."""
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _update_user_password(app, user_id: str, password: str) -> None:
    """Validate + persist a password change from the admin console."""
    app.state.auth.validate_password(password)
    await app.state.auth.validate_password_not_reused(user_id, password)
    password_hash = await asyncio.to_thread(auth.hash_password, password)
    await app.state.model.users.update_password(user_id, password_hash)


async def _update_user_email(app, user_id: str, email: str) -> None:
    """Apply an email change: 400 on a malformed address or one already
    used by another account — the same checks change-email applies."""
    auth.validate_email(email)
    existing = await app.state.model.users.get_user_by_email(email)
    if existing is not None and existing["id"] != user_id:
        raise HTTPException(status_code=400, detail="Email already in use")
    try:
        await app.state.model.users.update_email(user_id, email)
    except SAIntegrityError:
        # The pre-check above is not atomic with the UPDATE (#3101's
        # TOCTOU family): a concurrent registration or change-email can
        # claim the address in between. Map the race to the same 400 the
        # pre-check returns instead of an unhandled 500 (#3097).
        raise HTTPException(status_code=400, detail="Email already in use")


async def _update_user_handle(app, user_id: str, handle: str) -> None:
    """Set + propagate a handle change to live WS sessions; 400 on an
    invalid handle."""
    try:
        await app.state.model.users.set_user_handle(user_id, handle)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await wshandler.refresh_user_handle(app.state.sockets, user_id, handle)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    await _require_user(app, user_id)
    if req.email is not None:
        await _update_user_email(app, user_id, req.email)
    if req.password is not None:
        await _update_user_password(app, user_id, req.password)
    if req.handle is not None:
        await _update_user_handle(app, user_id, req.handle)
    if req.disabled is not None:
        await _update_user_disabled(app, req, user_id, admin)
    return {"status": "updated"}


def _reject_self_disable(
    req: UpdateUserRequest, user_id: str, admin: dict
) -> None:
    """An admin must not disable their own account (#2588) — the
    only accounts that can re-enable are the admin group's."""
    if req.disabled and user_id == admin["id"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot disable your own account",
        )


async def _update_user_disabled(
    app, req: UpdateUserRequest, user_id: str, admin: dict
) -> None:
    """Apply a disabled flag: guard self-disable, persist, and cut the
    user's live connections when disabling (#2588)."""
    _reject_self_disable(req, user_id, admin)
    try:
        updated = await app.state.model.users.set_user_disabled(
            user_id, req.disabled
        )
    except AgentPrincipalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:  # pragma: no cover — race between get and update
        raise HTTPException(status_code=404, detail="User not found")
    if req.disabled:
        # Cut the user's live connections too (#2588 review): the
        # WS is the terminal/control data plane, and a disabled
        # account must not keep it. 4001 -> the client logs out
        # rather than reconnect-looping.
        kicked = await wshandler.disconnect_user(
            app.state.sockets, user_id, reason="Account disabled"
        )
        if kicked:
            logger.info(
                "admin: disabled user %s; closed %d live connection(s)",
                user_id,
                kicked,
            )


@router.post("/users/{user_id}/unlockout")
async def unlock_user(
    user_id: str,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    """Reset a user's login lockout so they can log in immediately."""
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.login_attempts.clear_login_attempts(user["email"])
    return {"status": "unlocked"}


@router.get("/users/{user_id}/sessions")
async def list_user_sessions(
    user_id: str,
    admin: dict = Depends(acl.has_permission("manage-users")),
    app=Depends(get_app_dep),
):
    """List a user's active sessions with workstation identity (#2586).

    The queryable half of concurrent-logon auditing: one row per
    unexpired session, oldest first, carrying when it was established,
    when it expires, and the effective client IP / user agent it came
    from. The logon-time audit record (server log) fires when a new
    login is concurrent with a session from a different workstation;
    this endpoint is how an operator reviews the trail afterwards.
    """
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.sessions.purge_expired()
    rows = await app.state.model.sessions.list_sessions(user_id)
    return {
        "items": [
            {
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "source_ip": row["source_ip"],
                "user_agent": row["user_agent"],
            }
            for row in rows
        ]
    }


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


async def get_group_or_404(app, group_id: str) -> dict:
    """Fetch a group or raise the shared 404 (every group endpoint)."""
    group = await app.state.model.users.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def update_group_fields(app, group_id: str, req) -> dict:
    """Apply a group PATCH (both the permission-gated and admin routes)."""
    await get_group_or_404(app, group_id)
    updated = await app.state.model.users.update_group(
        group_id, name=req.name, description=req.description
    )
    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "updated"}


@router.get("/groups")
async def list_groups(
    page: int = 1,
    page_size: int = 10,
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
    source: str | None = None,
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    """List groups (#2944): one surface for every reader — pickers,
    share dialogs, and the admin Groups tab all read here, so the
    listing is authenticated rather than manage-groups-gated (a
    manage-groups delegate without manage-users still needs it).

    Returns the paged envelope ``{groups, page, page_size, total}``
    (#2750). ``source=manual`` hides the seeded per-workspace role
    groups; ``source=workspace-role`` shows only them; the default
    shows all. Writes (create/edit/delete, members) on this tree are
    gated ``manage-groups``.
    """
    if source is not None and source not in GROUP_SOURCES:
        raise HTTPException(
            status_code=422,
            detail="source must be one of: manual, workspace-role",
        )
    return await app.state.model.users.list_groups(
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        q=q,
        source=source,
    )


# --- Group management writes (single surface, #2941-fold; moved to
# /groups in #2944) ---


@router.post("/groups")
async def create_group(
    req: CreateGroupRequest,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    existing = await app.state.model.users.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await app.state.model.users.create_group(req.name, req.description)
    return group


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    req: UpdateGroupRequest,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    return await update_group_fields(app, group_id, req)


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    await get_group_or_404(app, group_id)
    await app.state.model.users.delete_group(group_id)
    # Clean up the group's ACL entries (ported from the removed
    # DELETE /groups — the admin variant used to orphan them).
    await app.state.model.acl.delete_acl_entries_for_resource(
        f"/groups/{group_id}"
    )
    return {"status": "deleted"}


@router.get("/groups/{group_id}/members")
async def list_group_members(
    group_id: str,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    await get_group_or_404(app, group_id)
    return await app.state.model.users.get_group_members(group_id)


@router.post("/groups/{group_id}/members")
async def add_group_member(
    group_id: str,
    req: AddGroupMemberRequest,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    await get_group_or_404(app, group_id)
    user = await app.state.model.users.get_user_by_id(req.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.users.add_user_to_group(req.user_id, group_id)
    return {"status": "added"}


@router.delete("/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    app=Depends(get_app_dep),
):
    removed = await app.state.model.users.remove_user_from_group(
        user_id, group_id
    )
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    return {"status": "removed"}


# --- ACL management endpoints ---


@router.get("/acl/tree")
async def get_acl_tree(
    admin: dict = Depends(acl.has_permission("manage-acls")),
    app=Depends(get_app_dep),
):
    return await app.state.model.acl.get_acl_tree_summary()


@router.get("/acl/by-principal/user/{user_id}")
async def get_acl_by_user(
    user_id: str,
    admin: dict = Depends(acl.has_permission("manage-acls")),
    app=Depends(get_app_dep),
):
    return await app.state.model.acl.get_acl_entries_by_principal_user(user_id)


@router.get("/acl/by-principal/group/{group_id}")
async def get_acl_by_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("manage-acls")),
    app=Depends(get_app_dep),
):
    return await app.state.model.acl.get_acl_entries_by_principal_group(
        group_id
    )


@router.get("/acl/resource")
async def get_resource_acl(
    resource: str,
    admin: dict = Depends(acl.has_permission("manage-acls")),
    app=Depends(get_app_dep),
):
    """Get resolved ACL entries for any resource (admin only)."""
    return await app.state.model.acl.get_acl_entries_resolved(resource)


def workspace_scope(resource: str) -> str | None:
    """The workspace node an ACL write targets, if any (#2764).

    ``/workspaces/{id}`` and deeper paths normalize to the workspace
    node; the ``/workspaces`` collection and non-workspace resources
    return ``None`` (site ``admin`` alone governs those — the admin ACL
    page edits only collection/static resources).
    """
    parts = resource.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "workspaces" and parts[1]:
        return f"/workspaces/{parts[1]}"
    return None


@router.put("/acl/resource")
async def replace_resource_acl(
    resource: str,
    entries: list[WorkspaceAclEntry],
    admin: dict = Depends(acl.has_permission("manage-acls")),
    app=Depends(get_app_dep),
):
    """Replace ACL entries for any resource (admin only).

    #2764: when the target is an individual workspace, the write also
    requires ``share-advanced`` on it — the same resource-level gate as
    ``PUT /workspaces/{id}/acl`` — so a raw ACE rewrite of a workspace
    always carries the workspace's own grant.
    """
    _validate_root_acl(entries, resource)

    workspace = workspace_scope(resource)
    if workspace is not None:
        principals = await app.state.acl.get_principals(admin["id"])
        if not await app.state.acl.check_permission(
            workspace, principals, "share-advanced"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"share-advanced permission required on {workspace}",
            )

    acl_entries = serialize_acl_entries(entries)
    await app.state.model.acl.replace_acl_entries(resource, acl_entries)
    return await app.state.model.acl.get_acl_entries_resolved(resource)


class ServerScheduleRequest(BaseModel):
    """Body for scheduling a server stop/recycle (#2661)."""

    action: str  # "stop" | "recycle"
    at: str | None = None  # absolute ISO-8601 timestamp
    in_seconds: float | None = None  # relative delay, > 0


@router.post("/server/schedule")
async def schedule_server_action(
    payload: ServerScheduleRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-server-schedule")),
):
    """Schedule a server stop or recycle for a future time (#2661).

    Provide ``at`` (absolute ISO-8601; naive = UTC) or ``in_seconds``
    (positive delay). The schedule persists across klangkd restarts.
    When it fires: a **stop** runs the graceful TERM/INT path and the
    process exits (code 0) — the service manager owns what happens
    next; a **recycle** runs the SIGHUP graceful restart in-process
    (listener and DB stay up) and never exits. In both, workspaces are
    drained gracefully and every connected client sees a live countdown.
    """
    app = request.app
    try:
        fire_at = resolve_fire_at(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    try:
        schedule = await app.state.model.server_schedules.create_schedule(
            payload.action, fire_at, created_by=admin["id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await app.state.server_scheduler.notify_pending()
    return schedule


@router.get("/server/schedule")
async def list_server_schedules(
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-server-schedule")),
):
    """List pending server stop/recycle schedules (#2661)."""
    return {
        "schedules": await request.app.state.model.server_schedules.pending_schedules()
    }


@router.delete("/server/schedule/{schedule_id}")
async def cancel_server_schedule(
    schedule_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-server-schedule")),
):
    """Cancel a pending server stop/recycle schedule (#2661)."""
    app = request.app
    cancelled = await app.state.model.server_schedules.cancel_schedule(
        schedule_id
    )
    if not cancelled:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await app.state.server_scheduler.notify_pending()
    return {"cancelled": schedule_id}


# --- Container lifecycle events (#2923) ---


async def _workspace_names(app, rows: list[dict]) -> dict:
    """One workspace-name lookup per distinct workspace id in the page;
    a deleted workspace yields ``None`` and the client falls back to
    the raw id."""
    names: dict[str, str | None] = {}
    for row in rows:
        wid = row["workspace_id"]
        if wid not in names:
            ws = await app.state.model.workspaces.get_workspace(wid)
            names[wid] = ws["name"] if ws else None
    return names


async def _actor_emails(app, rows: list[dict]) -> dict:
    """One email lookup per distinct human actor id in the page. A
    purged user yields ``None``; system/agent actors carry no email by
    construction."""
    emails: dict[str, str | None] = {}
    for row in rows:
        actor_id = row["actor_id"]
        if row["actor_type"] == "user" and actor_id not in emails:
            user = await app.state.model.users.get_user_by_id(actor_id)
            emails[actor_id] = user["email"] if user else None
    return emails


async def _annotate_events(app, rows: list[dict]) -> list[dict]:
    """Resolve workspace names and actor emails for one page of events
    (cosmetic joins bounded by the page size)."""
    names = await _workspace_names(app, rows)
    emails = await _actor_emails(app, rows)
    return [
        {
            **row,
            "workspace_name": names.get(row["workspace_id"]),
            "actor_email": emails.get(row["actor_id"]),
        }
        for row in rows
    ]


@router.get("/events")
async def list_container_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    workspace: str | None = None,
    workspace_id: str | None = None,
    viewer: dict = Depends(acl.has_permission("manage-events")),
):
    """Paged container start/stop history (#2923).

    Newest-first rows from the ``container_events`` audit table
    (#2915) plus the filter-matching total, optionally narrowed to one
    workspace: ``workspace`` (#3006) matches an exact workspace id or a
    workspace-name substring, while the legacy ``workspace_id`` stays
    an exact-id match. Gated on the dedicated ``manage-events``
    permission over the URL-derived resource ``/events``: the admin
    group holds it through the seeded Allow row, and a non-admin gets
    it only via an explicit ACE on that resource — read-only audit
    access without full admin.
    """
    app = request.app
    rows = await app.state.model.container_events.list_events(
        workspace_id=workspace_id,
        workspace=workspace,
        limit=limit,
        offset=offset,
    )
    total = await app.state.model.container_events.count_events(
        workspace_id=workspace_id, workspace=workspace
    )
    return {
        "items": await _annotate_events(app, rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }
