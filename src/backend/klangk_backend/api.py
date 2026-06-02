"""API route handlers for Klangk backend."""

import io
import logging
import posixpath
import sqlite3
import time
import uuid
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import (
    auth,
    container,
    emailsvc,
    files,
    wshandler,
    model,
    workspaces,
)
from .util import derive_hosting_info, resolve_env_secret

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


# --- Test/debug endpoints (only when KLANGK_TEST_MODE is set) ---

if resolve_env_secret("KLANGK_TEST_MODE"):  # pragma: no cover

    @router.get("/api/test/idle-timeout")
    async def get_idle_timeout(workspace_id: str | None = None):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": container.registry.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {"idle_timeout_seconds": container.IDLE_TIMEOUT_SECONDS}

    class SetIdleTimeoutRequest(BaseModel):
        seconds: int
        workspace_id: str | None = None

    @router.post("/api/test/set-idle-timeout")
    async def set_idle_timeout(body: SetIdleTimeoutRequest):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        seconds = body.seconds
        workspace_id = body.workspace_id
        if workspace_id:
            container.registry.set_workspace_idle_timeout(
                workspace_id, seconds
            )
        else:
            container.IDLE_TIMEOUT_SECONDS = seconds
            container.CHECK_INTERVAL_SECONDS = max(10, min(60, seconds // 3))
        return {"idle_timeout_seconds": seconds}


# --- Config endpoint ---

SOLIPLEX_URL = resolve_env_secret("SOLIPLEX_URL", "")


@router.get("/api/config")
async def get_config():
    return {
        "soliplex_url": SOLIPLEX_URL,
        "registration_enabled": auth.registration_enabled(),
    }


# --- Auth endpoints ---


@router.post("/auth/register")
async def register(
    req: auth.RegisterRequest,
    request: Request,
):
    if resolve_env_secret("KLANGK_TEST_MODE"):
        # Test mode: auto-verify so E2E tests get immediate access
        result = await auth.register(req, verified=True)
        return result

    logger.info("Registering user: %s", req.email)
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    if len(req.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )

    password_hash = auth.hash_password(req.password)
    user_id = str(uuid.uuid4())

    hostname, proto, base_path = derive_hosting_info(request.headers)
    logger.info(
        "Hosting info: hostname=%s proto=%s base_path=%s",
        hostname,
        proto,
        base_path,
    )
    verification_token = auth.create_verification_token(user_id)
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    logger.info("Verification URL: %s", verification_url)

    # Insert user and send email in a transaction — if the email fails,
    # the user insert is rolled back so they can try again.
    async with model.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified) VALUES (?, ?, ?, 0)",
            (user_id, req.email, password_hash),
        )
        logger.info("User inserted (uncommitted): %s", req.email)
        await emailsvc.send_verification_email(req.email, verification_url)
        logger.info("Verification email sent, committing user: %s", req.email)

    return {"status": "pending_verification", "email": req.email}


@router.get("/auth/verify")
async def verify_email(token: str):
    """Verify a user's email via the token from the verification link."""
    user_id = auth.decode_verification_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification token"
        )
    updated = await model.verify_user(user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    user = await model.get_user_by_id(user_id)
    roles = await model.get_user_roles(user_id)
    access_token = auth.create_token(user_id, user["email"], roles)
    return {"status": "verified", "access_token": access_token}


_resend_timestamps: dict[str, float] = {}
RESEND_COOLDOWN_SECONDS = 60


@router.post("/auth/resend-verification")
async def resend_verification(
    req: auth.LoginRequest,
    request: Request,
):
    """Resend verification email. Requires email+password to prevent abuse."""
    user = await model.get_user_by_email(req.email)
    if user is None or not auth.verify_password(
        req.password, user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("verified"):
        raise HTTPException(status_code=400, detail="Account already verified")

    # Rate limit: one resend per email per minute
    now = time.time()
    last = _resend_timestamps.get(req.email, 0)
    if now - last < RESEND_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    _resend_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(request.headers)
    verification_token = auth.create_verification_token(user["id"])
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    await emailsvc.send_verification_email(req.email, verification_url)
    return {"status": "sent"}


class ForgotPasswordRequest(auth.BaseModel):
    email: str


_reset_timestamps: dict[str, float] = {}
RESET_COOLDOWN_SECONDS = 60


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Send a password reset email if the account exists."""
    user = await model.get_user_by_email(req.email)
    if user is None:
        # Don't reveal whether the email exists
        return {"status": "sent"}

    # Rate limit: one reset email per address per minute
    now = time.time()
    last = _reset_timestamps.get(req.email, 0)
    if now - last < RESET_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    _reset_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(request.headers)
    reset_token = auth.create_password_reset_token(user["id"])
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    await emailsvc.send_password_reset_email(req.email, reset_url)
    return {"status": "sent"}


class ResetPasswordRequest(auth.BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    """Reset password using a token from the reset email."""
    user_id = auth.decode_password_reset_token(req.token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )
    if len(req.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )
    password_hash = auth.hash_password(req.password)
    await model.update_password(user_id, password_hash)
    # Auto-login after reset
    user = await model.get_user_by_id(user_id)
    if user is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="User not found")
    roles = await model.get_user_roles(user_id)
    token = auth.create_token(user_id, user["email"], roles)
    return {"status": "reset", "access_token": token}


@router.post("/auth/login", response_model=auth.TokenResponse)
async def login(req: auth.LoginRequest):
    return await auth.login(req)


class ChangePasswordRequest(auth.BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: dict = Depends(auth.get_current_user),
):
    """Change password. Requires current password."""
    stored = await model.get_user_by_email(user["email"])
    if stored is None or not auth.verify_password(
        req.current_password, stored["password_hash"]
    ):
        raise HTTPException(
            status_code=401, detail="Current password is incorrect"
        )
    if len(req.new_password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )
    password_hash = auth.hash_password(req.new_password)
    await model.update_password(user["id"], password_hash)
    return {"status": "updated"}


class ChangeEmailRequest(auth.BaseModel):
    email: str
    password: str


@router.post("/auth/change-email")
async def change_email(
    req: ChangeEmailRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    """Change email. Requires password. Marks account as unverified."""
    stored = await model.get_user_by_email(user["email"])
    if stored is None or not auth.verify_password(
        req.password, stored["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None and existing["id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Email already in use")
    await model.update_email(user["id"], req.email)
    # Mark as unverified and send verification email
    async with model.transaction() as db:
        await db.execute(
            "UPDATE users SET verified = 0 WHERE id = ?",
            (user["id"],),
        )

    hostname, proto, base_path = derive_hosting_info(request.headers)
    token = auth.create_verification_token(user["id"])
    url = f"{proto}://{hostname}{base_path}/#/verify?token={token}"
    await emailsvc.send_verification_email(req.email, url)
    return {"status": "updated", "needs_verification": True}


@router.post("/auth/logout")
async def logout(
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    await wshandler.state.logout_user(user["id"])
    # Blocklist the token so it can't be reused after logout
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        await auth.logout(authorization[7:])
    return {"status": "ok"}


# --- Workspace endpoints ---


@router.get("/workspaces")
async def list_workspaces(user: dict = Depends(auth.get_current_user)):
    return await workspaces.list_workspaces(user["id"])


@router.get("/workspaces/shared")
async def list_shared_workspaces(user: dict = Depends(auth.get_current_user)):
    return await model.list_shared_workspaces(user["id"])


class CreateWorkspaceRequest(BaseModel):
    name: str
    image: str | None = None
    default_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.get("/images")
async def list_images(_user: dict = Depends(auth.get_current_user)):
    return {
        "default": container.IMAGE_NAME,
        "allowed": sorted(container.ALLOWED_IMAGES),
    }


# --- Volume management ---


@router.get("/volumes")
async def list_volumes(_user: dict = Depends(auth.get_current_user)):
    docker = await container.registry.get_docker()
    volumes = await docker.volumes.list(
        filters={"label": [f"klangk.instance={container.INSTANCE_ID}"]}
    )
    return [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
        }
        for v in volumes.get("Volumes") or []
    ]


class CreateVolumeRequest(BaseModel):
    name: str


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    _user: dict = Depends(auth.get_current_user),
):
    docker = await container.registry.get_docker()
    try:
        existing = await docker.volumes.get(body.name)
        await existing.show()  # raises 404 if not found
        raise HTTPException(
            status_code=409, detail=f"Volume {body.name!r} already exists"
        )
    except container.aiodocker.exceptions.DockerError as e:
        if e.status != 404:
            raise
    vol = await docker.volumes.create(
        {
            "Name": body.name,
            "Labels": {
                "klangk.managed": "true",
                "klangk.instance": container.INSTANCE_ID,
            },
        }
    )
    info = await vol.show()
    return {"name": info["Name"], "created": info.get("CreatedAt", "")}


@router.delete("/volumes/{name}")
async def delete_volume(
    name: str, _user: dict = Depends(auth.get_current_user)
):
    docker = await container.registry.get_docker()
    try:
        vol = await docker.volumes.get(name)
        info = await vol.show()
        labels = info.get("Labels") or {}
        if labels.get("klangk.instance") != container.INSTANCE_ID:
            raise HTTPException(
                status_code=404,
                detail="Volume not managed by this Klangk instance",
            )
        await vol.delete()
    except container.aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404, detail="Volume not found"
            ) from None
        if e.status == 409:
            raise HTTPException(
                status_code=409, detail="Volume is in use"
            ) from None
        raise
    return {"status": "deleted"}


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspaceRequest, user: dict = Depends(auth.get_current_user)
):
    if body.image and body.image not in container.ALLOWED_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Image {body.image!r} is not allowed. "
            f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
        )
    if body.mounts:
        mount_err = container.validate_mounts(body.mounts)
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    try:
        return await workspaces.create_workspace(
            user["id"],
            body.name,
            image=body.image,
            default_command=body.default_command,
            mounts=body.mounts,
            env=body.env,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    except OSError as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(e))


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    image: str | None = None
    default_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    user: dict = Depends(auth.get_current_user),
):
    fields = body.model_dump(exclude_unset=True)
    if "image" in fields and fields["image"] is not None:
        if fields["image"] not in container.ALLOWED_IMAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Image {fields['image']!r} is not allowed. "
                f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
            )
    if "mounts" in fields and fields["mounts"]:
        mount_err = container.validate_mounts(fields["mounts"])
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    updated = await model.update_workspace(workspace_id, user["id"], **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if "default_command" in fields:
        workspaces.write_default_command(
            user["id"], workspace_id, fields["default_command"]
        )
    return {"status": "updated"}


class DuplicateWorkspaceRequest(BaseModel):
    name: str


@router.post("/workspaces/{workspace_id}/duplicate")
async def duplicate_workspace(
    workspace_id: str,
    body: DuplicateWorkspaceRequest,
    user: dict = Depends(auth.get_current_user),
):
    source = await workspaces.get_workspace(workspace_id, user["id"])
    if source is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return await workspaces.create_workspace(
            user["id"],
            body.name,
            image=source.get("image"),
            default_command=source.get("default_command"),
            mounts=source.get("mounts"),
            env=source.get("env"),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, user: dict = Depends(auth.get_current_user)
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Prefer the live container_id from the registry (tracks the currently
    # running container) over the DB value (may be stale if the container
    # was already stopped by idle timeout).
    live_state = container.registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    if cid:
        await container.registry.stop_and_remove_container(cid)
    await wshandler.reset_workspace_state(workspace_id)

    deleted = await workspaces.delete_workspace(workspace_id, user["id"])
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "deleted"}


# --- Workspace sharing endpoints ---


@router.get("/workspaces/{workspace_id}/members")
async def get_workspace_members(
    workspace_id: str, user: dict = Depends(auth.get_current_user)
):
    workspace = await model.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace["user_id"] != user["id"]:
        raise HTTPException(
            status_code=403, detail="Only the owner can manage members"
        )
    return await model.get_workspace_members(workspace_id)


class AddMemberRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str,
    body: AddMemberRequest,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await model.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace["user_id"] != user["id"]:
        raise HTTPException(
            status_code=403, detail="Only the owner can manage members"
        )
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(
            status_code=400, detail="Cannot share with yourself"
        )
    await model.share_workspace(workspace_id, target["id"])
    return {
        "status": "shared",
        "user_id": target["id"],
        "email": target["email"],
    }


@router.delete("/workspaces/{workspace_id}/members/{member_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await model.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace["user_id"] != user["id"]:
        raise HTTPException(
            status_code=403, detail="Only the owner can manage members"
        )
    await model.unshare_workspace(workspace_id, member_id)
    return {"status": "removed"}


# --- User search endpoint ---


@router.get("/users/search")
async def search_users(
    q: str,
    _user: dict = Depends(auth.get_current_user),
):
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="Query too short")
    return await model.search_users(q)


# --- File endpoints ---


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = ".",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return files.list_files(workspace["user_id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        content = files.read_file(workspace["user_id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(
            status_code=404, detail="File not found or too large"
        )
    return {"path": path, "content": content}


@router.delete("/workspaces/{workspace_id}/files")
async def delete_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        deleted = files.delete_path(workspace["user_id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    return {"path": deleted, "status": "deleted"}


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    body: RenameFileRequest,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        renamed = files.rename_path(
            workspace["user_id"], workspace_id, body.old_path, body.new_path
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source not found")
    except FileExistsError:
        raise HTTPException(
            status_code=409, detail="Destination already exists"
        )
    return {"path": renamed, "status": "renamed"}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        resolved = files.resolve_path(workspace["user_id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if resolved.is_file():
        return FileResponse(resolved, filename=resolved.name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in resolved.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(resolved))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{resolved.name}.zip"'
        },
    )


@router.post("/workspaces/{workspace_id}/files/upload")
async def upload_file(
    workspace_id: str,
    file: UploadFile,
    path: str = "",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:  # pragma: no cover
        raise HTTPException(status_code=400, detail="No filename provided")

    container_id = workspace.get("container_id")
    if container_id is not None:
        container.registry.record_activity(container_id)

    content = await file.read()
    try:
        saved_path = files.write_file(
            workspace["user_id"], workspace_id, filename, content
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}


# --- Browser bridge endpoint ---


class BrowserDelegateRequest(BaseModel):
    model_config = {"extra": "allow"}
    action: str
    token: str


@router.post("/api/browser-delegate")
async def browser_delegate(body: BrowserDelegateRequest):
    """Bridge endpoint for Pi extensions to delegate actions to the browser.

    The container calls this endpoint with a bridge token (set as
    KLANGK_BRIDGE_TOKEN in the container env). The backend resolves the
    token to a workspace_id (and optionally a specific browser connection),
    relays the request to the Flutter client over WebSocket, and returns
    the browser's response.
    """
    resolved = container.registry.resolve_bridge_token(body.token)
    if resolved is None:
        raise HTTPException(status_code=403, detail="Invalid bridge token")
    workspace_id, target_sock = resolved

    session = wshandler.state.get_session(workspace_id)
    if not session:
        raise HTTPException(
            status_code=502,
            detail="No browser client connected to this workspace",
        )

    payload = body.model_dump(exclude={"token"})

    if target_sock not in session.browser_subscribers:
        raise HTTPException(
            status_code=502,
            detail="Browser connection not available",
        )
    result = await session.dispatch_browser_request_to(target_sock, payload)

    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# --- Admin endpoints (require admin role) ---


@router.get("/admin/users")
async def list_users(admin: dict = Depends(auth.require_role("admin"))):
    return await model.list_users()


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str


@router.post("/admin/users")
async def admin_create_user(
    req: AdminCreateUserRequest,
    admin: dict = Depends(auth.require_role("admin")),
):
    """Create a verified user directly (admin only, no email verification)."""
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")
    if len(req.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )
    password_hash = auth.hash_password(req.password)
    user = await model.create_user(req.email, password_hash, verified=True)
    return {"id": user["id"], "email": user["email"], "status": "created"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str, admin: dict = Depends(auth.require_role("admin"))
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


@router.post("/admin/users/{user_id}/roles/{role_name}")
async def add_user_role(
    user_id: str,
    role_name: str,
    admin: dict = Depends(auth.require_role("admin")),
):
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.ensure_role(role_name)
    await model.assign_role(user_id, role_name)
    return {"status": "assigned", "user_id": user_id, "role": role_name}


@router.delete("/admin/users/{user_id}/roles/{role_name}")
async def remove_user_role(
    user_id: str,
    role_name: str,
    admin: dict = Depends(auth.require_role("admin")),
):
    removed = await model.remove_role(user_id, role_name)
    if not removed:
        raise HTTPException(
            status_code=404, detail="Role assignment not found"
        )
    return {"status": "removed", "user_id": user_id, "role": role_name}


class UpdateUserRequest(auth.BaseModel):
    email: str | None = None
    password: str | None = None


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: dict = Depends(auth.require_role("admin")),
):
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if req.email is not None:
        await model.update_email(user_id, req.email)
    if req.password is not None:
        password_hash = auth.hash_password(req.password)
        await model.update_password(user_id, password_hash)
    return {"status": "updated"}
