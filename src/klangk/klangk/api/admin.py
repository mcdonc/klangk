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
    stepup,
    wshandler,
)
from ..server_schedule import resolve_fire_at
from ..notifier import notify_event
from .common import get_app_dep, request_metadata
from ..model import (
    ACTION_ALLOW,
    AgentPrincipalError,
    GROUP_SOURCES,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
)
from ..model.merged_events import MergedEventFilters
from .common import (
    WorkspaceAclEntry,
    send_email,
    serialize_acl_entries,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def record_admin_event(
    app,
    request: Request,
    admin: dict,
    event: str,
    target_type: str,
    target_id: str | None,
    detail: dict | None,
) -> None:
    """Write one admin identity/privilege audit row (#3205).

    Every admin-route audit emit needs the same ingredients — the
    acting admin, the per-request HTTP metadata, and the event's
    target/detail — so they funnel through here. Best-effort by design
    (``record_best_effort``): an unwritable audit table is logged,
    never bricked onto account management.
    """
    source_ip, user_agent, method, referer = request_metadata(request)
    await app.state.model.audit_events.record_best_effort(
        event,
        actor_id=admin["id"],
        actor_email=admin["email"],
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    # SA/ISSO notification for the same lifecycle action (#3250). The
    # allowlist filters to the notify-worthy event names; everything
    # funneled through here (user CRUD, group/ACL changes) gets one
    # hook instead of per-route wiring. Fire-and-forget — never fails
    # the action (which has already succeeded).
    notify_event(
        app,
        event,
        actor_id=admin["id"],
        actor_email=admin["email"],
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        source_ip=source_ip,
    )


async def record_admin_user_event(
    app, request: Request, admin: dict, event: str, user_id: str, detail: dict
) -> None:
    """``record_admin_event`` with a user target (the common case)."""
    await record_admin_event(
        app, request, admin, event, "user", user_id, detail
    )


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
    _step_up: None = Depends(stepup.require_step_up()),
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
    _step_up: None = Depends(stepup.require_step_up()),
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
    _step_up: None = Depends(stepup.require_step_up()),
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
    _step_up: None = Depends(stepup.require_step_up()),
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
        verification_token = app.state.auth.create_verification_token(
            user_id, req.email
        )
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

        await record_admin_user_event(
            app,
            request,
            admin,
            "user.create",
            user_id,
            {"email": req.email, "status": "pending-verification"},
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
    # Admin-chosen password: force the user to change it on first
    # login (#3172). The flag lands in the same INSERT,
    # so no crash window can leave an unflagged admin-chosen password.
    user = await app.state.model.users.create_user(
        req.email, password_hash, verified=True, must_change_password=True
    )
    await record_admin_user_event(
        app,
        request,
        admin,
        "user.create",
        user["id"],
        {"email": req.email, "status": "created"},
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
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-users")),
    _step_up: None = Depends(stepup.require_step_up()),
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
    # Revoke every session while the rows still exist to read the JTIs
    # from (#3195): each token is blocklisted (401 "Token has been
    # revoked" on its next use) and the live sockets it opened are cut.
    await app.state.auth.revoke_all_user_sessions(
        user_id, reason="user deletion"
    )
    deleted = await app.state.model.users.delete_user(user_id)
    # Cut what remains by user id, after the row is gone (#3195): a
    # connect that raced in between the revoke and the delete must hit
    # the missing user row and die, and sockets from pre-session-registry
    # tokens carry no session row for the revoke to reach.
    await _kick_user_sockets(app, user_id, "deleted", "Account deleted")
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
    # The deletion is audited with the victim's email in the detail —
    # the user row (and its email) is gone, so the audit row is the
    # only place the identity survives (#3205).
    await record_admin_user_event(
        app,
        request,
        admin,
        "user.delete",
        user_id,
        {"email": user["email"]},
    )
    return {"status": "deleted"}


class UpdateUserRequest(auth.BaseModel):
    email: str | None = None
    password: str | None = None
    handle: str | None = None
    disabled: bool | None = None
    must_change_password: bool | None = None


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
    # Admin-chosen password: force the user to change it on next
    # login (#3172) — hash + flag land in one transaction.
    await app.state.model.users.set_password_force_change(
        user_id, password_hash
    )


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


async def _apply_user_field_updates(
    app, req: "UpdateUserRequest", user: dict
) -> None:
    """Apply simple field updates from an admin user-update request."""
    user_id = user["id"]
    if req.email is not None:
        await _update_user_email(app, user_id, req.email)
    if req.password is not None:
        await _update_user_password(app, user_id, req.password)
    if req.handle is not None:
        await _update_user_handle(app, user_id, req.handle)
    if req.must_change_password is not None:
        await _update_must_change_password(app, user, req.must_change_password)


async def _update_must_change_password(app, user: dict, flag: bool) -> None:
    """Set/clear the forced-change flag (#3172). Rejected for
    non-local accounts: an OIDC user cannot ever clear it (their
    passwords live with the IdP), so flagging one is a permanent
    lockout."""
    if user.get("provider") not in (None, "local"):
        raise HTTPException(
            status_code=400,
            detail=(
                "must_change_password applies only to local-password accounts"
            ),
        )
    await app.state.model.users.set_must_change_password(user["id"], flag)


# The admin-updatable fields, in the order UpdateUserRequest declares
# them — the audit detail names which of them a PATCH carried (#3205).
_UPDATABLE_USER_FIELDS = (
    "email",
    "password",
    "handle",
    "disabled",
    "must_change_password",
)


async def _notify_disabled_toggle(
    app,
    request: Request,
    req: "UpdateUserRequest",
    user: dict,
    user_id: str,
    admin: dict,
) -> None:
    """Notify one disable/enable under its own event name — the STIG
    rules distinguish them (SV-222419 / SV-222422), and the audit
    stream records the toggle only inside user.update (#3250)."""
    notify_event(
        app,
        "user.disable" if req.disabled else "user.enable",
        actor_id=admin["id"],
        actor_email=admin["email"],
        target_type="user",
        target_id=user_id,
        # The PATCH may carry email + disabled together; report the
        # address the row now has (a change also fires its own
        # user.email.change notification).
        detail={"email": req.email or user["email"]},
        source_ip=request_metadata(request)[0],
    )


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-users")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    user = await _require_user(app, user_id)
    await _apply_user_field_updates(app, req, user)
    if req.disabled is not None:
        await _update_user_disabled(app, req, user_id, admin)
        await _notify_disabled_toggle(app, request, req, user, user_id, admin)
    changed = [
        f for f in _UPDATABLE_USER_FIELDS if getattr(req, f) is not None
    ]
    await record_admin_user_event(
        app,
        request,
        admin,
        "user.update",
        user_id,
        {"fields": changed},
    )
    await record_admin_credential_events(app, request, admin, req, user_id)
    return {"status": "updated"}


async def record_admin_credential_events(
    app, request: Request, admin: dict, req: "UpdateUserRequest", user_id: str
) -> None:
    """Emit the specific credential events an admin PATCH carried.

    The self-service paths emit ``user.password.change`` /
    ``user.email.change`` directly, so an incident query on those names
    must see admin-forced changes too (#3205 review) — not just the
    ``user.update`` row the PATCH always writes.
    """
    if req.password is not None:
        await record_admin_user_event(
            app,
            request,
            admin,
            "user.password.change",
            user_id,
            {"via": "admin"},
        )
    if req.email is not None:
        await record_admin_user_event(
            app,
            request,
            admin,
            "user.email.change",
            user_id,
            {"email": req.email, "via": "admin"},
        )


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


async def _kick_user_sockets(
    app, user_id: str, action: str, reason: str
) -> None:
    """Cut a user's live sockets (#2588, #3162).

    The main /ws connections (the terminal/control data plane) and the
    consent-decider sockets (egress-consent authority) alike; 4001 ->
    the clients log out rather than reconnect-looping. *action* names
    the trigger in the log line ("disabled", "deleted").
    """
    kicked = await wshandler.disconnect_user(
        app.state.sockets, user_id, reason=reason
    )
    deciders_kicked = await wshandler.disconnect_deciders_by_user(
        app, user_id, reason=reason
    )
    if kicked or deciders_kicked:
        logger.info(
            "admin: %s user %s; closed %d live connection(s)"
            " and %d consent decider(s)",
            action,
            user_id,
            kicked,
            deciders_kicked,
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
        await _kick_user_sockets(app, user_id, "disabled", "Account disabled")


@router.post("/users/{user_id}/unlockout")
async def unlock_user(
    user_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-users")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    """Reset a user's login lockout so they can log in immediately."""
    user = await app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.login_attempts.clear_login_attempts(user["email"])
    await record_admin_user_event(
        app, request, admin, "user.unlock", user_id, {"email": user["email"]}
    )
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
    when it expires, the effective client IP / user agent it came
    from, and when it was last seen active (#3151 — the clock the idle
    timeout judges). The logon-time audit record (server log) fires
    when a new login is concurrent with a session from a different
    workstation; this endpoint is how an operator reviews the trail
    afterwards.
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
                "last_seen_at": row["last_seen_at"],
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
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    existing = await app.state.model.users.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await app.state.model.users.create_group(req.name, req.description)
    await record_admin_event(
        app,
        request,
        admin,
        "group.create",
        "group",
        group["id"],
        {"name": req.name},
    )
    return group


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    req: UpdateGroupRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    result = await update_group_fields(app, group_id, req)
    await record_admin_event(
        app,
        request,
        admin,
        "group.update",
        "group",
        group_id,
        {"name": req.name, "description": req.description},
    )
    return result


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    group = await get_group_or_404(app, group_id)
    await app.state.model.users.delete_group(group_id)
    # Clean up the group's ACL entries (ported from the removed
    # DELETE /groups — the admin variant used to orphan them).
    await app.state.model.acl.delete_acl_entries_for_resource(
        f"/groups/{group_id}"
    )
    await record_admin_event(
        app,
        request,
        admin,
        "group.delete",
        "group",
        group_id,
        {"name": group["name"]},
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
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    await get_group_or_404(app, group_id)
    user = await app.state.model.users.get_user_by_id(req.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.users.add_user_to_group(req.user_id, group_id)
    await record_admin_event(
        app,
        request,
        admin,
        "group.member.add",
        "group",
        group_id,
        {"user_id": req.user_id},
    )
    return {"status": "added"}


@router.delete("/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-groups")),
    _step_up: None = Depends(stepup.require_step_up()),
    app=Depends(get_app_dep),
):
    removed = await app.state.model.users.remove_user_from_group(
        user_id, group_id
    )
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    await record_admin_event(
        app,
        request,
        admin,
        "group.member.remove",
        "group",
        group_id,
        {"user_id": user_id},
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
    request: Request,
    admin: dict = Depends(acl.has_permission("manage-acls")),
    _step_up: None = Depends(stepup.require_step_up()),
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
    await record_admin_event(
        app,
        request,
        admin,
        "acl.replace",
        "acl",
        resource,
        {"entries": len(entries)},
    )
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
    _step_up: None = Depends(stepup.require_step_up()),
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
    _step_up: None = Depends(stepup.require_step_up()),
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
            # The raw hmac tag is verification-internal (#3174); the
            # admin list view never needs it on the wire.
            **{k: v for k, v in row.items() if k != "hmac"},
            "workspace_name": names.get(row["workspace_id"]),
            "actor_email": emails.get(row["actor_id"]),
        }
        for row in rows
    ]


@router.get("/events/audit")
async def list_audit_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    viewer: dict = Depends(acl.has_permission("manage-events")),
):
    """Paged identity/privilege audit history (#3205).

    Newest-first rows from the ``audit_events`` table plus the
    filter-matching total. ``event`` / ``actor`` / ``target`` are
    optional substrings (event name; actor id or email; target id).
    The raw ``hmac`` tag is verification-internal (#3174) and never
    ships on the wire. Gated on ``manage-events`` — the same
    permission as the container-events view, so a delegate granted
    read-only audit access sees both streams.
    """
    app = request.app
    rows = await app.state.model.audit_events.list_events(
        event=event, actor=actor, target=target, limit=limit, offset=offset
    )
    total = await app.state.model.audit_events.count_events(
        event=event, actor=actor, target=target
    )
    return {
        "items": [
            {k: v for k, v in row.items() if k != "hmac"} for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/events/containers")
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


# --- Time-correlated merged stream (#3251) ---


async def _merged_workspace_names(app, rows: list[dict]) -> dict:
    """Workspace-id -> name for one merged page (cosmetic join
    bounded by the page size); ``None`` for a deleted workspace."""
    names: dict[str, str | None] = {}
    for row in rows:
        wid = row["workspace_id"]
        if wid is None or wid in names:
            continue
        ws = await app.state.model.workspaces.get_workspace(wid)
        names[wid] = ws["name"] if ws else None
    return names


def _needs_email_lookup(row: dict, emails: dict) -> bool:
    """Whether a merged row's actor needs a users-table email lookup:
    it is a human actor (``actor_type='user'`` — every branch's
    projection only stamps it on a set actor id, so system/agent
    rows and actor-less rows never match), and it arrived without a
    denormalized email (audit rows carry one; container and egress
    rows do not) that has not been resolved on this page yet."""
    return (
        row.get("actor_type") == "user"
        and not row["actor_email"]
        and row["actor_id"] not in emails
    )


async def _merged_actor_emails(app, rows: list[dict]) -> dict:
    """Actor-id -> email for one merged page's rows that need the
    lookup. A purged user yields ``None``."""
    emails: dict[str, str | None] = {}
    for row in rows:
        if not _needs_email_lookup(row, emails):
            continue
        user = await app.state.model.users.get_user_by_id(row["actor_id"])
        emails[row["actor_id"]] = user["email"] if user else None
    return emails


async def _annotate_merged_events(app, rows: list[dict]) -> list[dict]:
    """Resolve workspace names and actor emails for one merged page
    (the same cosmetic joins the container view applies, over the
    merged row shape)."""
    names = await _merged_workspace_names(app, rows)
    emails = await _merged_actor_emails(app, rows)
    return [
        {
            **row,
            "workspace_name": names.get(row["workspace_id"]),
            "actor_email": row["actor_email"] or emails.get(row["actor_id"]),
        }
        for row in rows
    ]


@router.get("/events")
async def list_merged_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    since: float | None = None,
    until: float | None = None,
    actor: str | None = None,
    workspace: str | None = None,
    event: str | None = None,
    viewer: dict = Depends(acl.has_permission("manage-events")),
):
    """Time-correlated audit stream across all three audit tables
    (#3251, SV-222439).

    One newest-first page over ``audit_events``, ``container_events``
    and ``egress_consent`` merged by timestamp, each item naming its
    origin in ``source`` (``audit`` / ``container`` / ``egress``) and
    embedding the full origin row in ``data`` (the HMAC tag, #3174,
    is verification-internal and never ships). Filters: ``since`` /
    ``until`` (inclusive epoch seconds), ``actor`` (id or email
    substring), ``workspace`` (exact id or name substring), ``event``
    (name substring; consent rows are named ``egress.<decision>``).
    Gated on ``manage-events`` — the same permission as the two
    per-table views, so one read-only audit grant covers every
    stream.
    """
    app = request.app
    filters = MergedEventFilters(
        since=since,
        until=until,
        actor=actor,
        workspace=workspace,
        event=event,
    )
    rows = await app.state.model.merged_events.list_events(
        filters, limit=limit, offset=offset
    )
    total = await app.state.model.merged_events.count_events(filters)
    return {
        "items": await _annotate_merged_events(app, rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }
