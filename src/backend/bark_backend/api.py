"""API route handlers for Bark backend."""

import io
import logging
import sqlite3
import time
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import (
    auth,
    container_manager,
    email_service,
    file_service,
    ws_handler,
    user_store,
    workspace_manager,
)
from .env_util import resolve_env_secret

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


# --- Test/debug endpoints (only when BARK_TEST_MODE is set) ---

if resolve_env_secret("BARK_TEST_MODE"):  # pragma: no cover

    @router.get("/api/test/idle-timeout")
    async def get_idle_timeout(workspace_id: str | None = None):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": container_manager.registry.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {"idle_timeout_seconds": container_manager.IDLE_TIMEOUT_SECONDS}

    @router.post("/api/test/set-idle-timeout")
    async def set_idle_timeout(seconds: int, workspace_id: str | None = None):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        if workspace_id:
            container_manager.registry.set_workspace_idle_timeout(
                workspace_id, seconds
            )
        else:
            container_manager.IDLE_TIMEOUT_SECONDS = seconds
            container_manager.CHECK_INTERVAL_SECONDS = max(
                10, min(60, seconds // 3)
            )
        return {"idle_timeout_seconds": seconds}


# --- Config endpoint ---

SOLIPLEX_URL = resolve_env_secret("SOLIPLEX_URL", "")


@router.get("/api/config")
async def get_config():
    return {"soliplex_url": SOLIPLEX_URL}


# --- Auth endpoints ---


@router.post("/auth/register")
async def register(
    req: auth.RegisterRequest,
    request: Request,
):
    if resolve_env_secret("BARK_TEST_MODE"):
        # Test mode: auto-verify so E2E tests get immediate access
        result = await auth.register(req, verified=True)
        return result
    import uuid

    logger.info("Registering user: %s", req.email)
    auth.validate_email(req.email)
    existing = await user_store.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    if len(req.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )

    password_hash = auth.hash_password(req.password)
    user_id = str(uuid.uuid4())

    hostname, proto, base_path = ws_handler.derive_hosting_info(
        request.headers
    )
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
    async with user_store.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified) VALUES (?, ?, ?, 0)",
            (user_id, req.email, password_hash),
        )
        logger.info("User inserted (uncommitted): %s", req.email)
        await email_service.send_verification_email(
            req.email, verification_url
        )
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
    updated = await user_store.verify_user(user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    user = await user_store.get_user_by_id(user_id)
    roles = await user_store.get_user_roles(user_id)
    token = auth.create_token(user_id, user["email"], roles)
    return {"status": "verified", "access_token": token}


_resend_timestamps: dict[str, float] = {}
RESEND_COOLDOWN_SECONDS = 60


@router.post("/auth/resend-verification")
async def resend_verification(
    req: auth.LoginRequest,
    request: Request,
):
    """Resend verification email. Requires email+password to prevent abuse."""
    user = await user_store.get_user_by_email(req.email)
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

    hostname, proto, base_path = ws_handler.derive_hosting_info(
        request.headers
    )
    verification_token = auth.create_verification_token(user["id"])
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    await email_service.send_verification_email(req.email, verification_url)
    return {"status": "sent"}


class ForgotPasswordRequest(auth.BaseModel):
    email: str


_reset_timestamps: dict[str, float] = {}
RESET_COOLDOWN_SECONDS = 60


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Send a password reset email if the account exists."""
    user = await user_store.get_user_by_email(req.email)
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

    hostname, proto, base_path = ws_handler.derive_hosting_info(
        request.headers
    )
    reset_token = auth.create_password_reset_token(user["id"])
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    await email_service.send_password_reset_email(req.email, reset_url)
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
    await user_store.update_password(user_id, password_hash)
    # Auto-login after reset
    user = await user_store.get_user_by_id(user_id)
    if user is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="User not found")
    roles = await user_store.get_user_roles(user_id)
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
    stored = await user_store.get_user_by_email(user["email"])
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
    await user_store.update_password(user["id"], password_hash)
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
    stored = await user_store.get_user_by_email(user["email"])
    if stored is None or not auth.verify_password(
        req.password, stored["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    auth.validate_email(req.email)
    existing = await user_store.get_user_by_email(req.email)
    if existing is not None and existing["id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Email already in use")
    await user_store.update_email(user["id"], req.email)
    # Mark as unverified and send verification email
    db = await user_store.get_db()
    try:
        await db.execute(
            "UPDATE users SET verified = 0 WHERE id = ?",
            (user["id"],),
        )
        await db.commit()
    finally:
        await db.close()

    hostname, proto, base_path = ws_handler.derive_hosting_info(
        request.headers
    )
    token = auth.create_verification_token(user["id"])
    url = f"{proto}://{hostname}{base_path}/#/verify?token={token}"
    await email_service.send_verification_email(req.email, url)
    return {"status": "updated", "needs_verification": True}


@router.post("/auth/logout")
async def logout(user: dict = Depends(auth.get_current_user)):
    await container_manager.registry.stop_user_containers(user["id"])
    return {"status": "ok"}


# --- Workspace endpoints ---


@router.get("/workspaces")
async def list_workspaces(user: dict = Depends(auth.get_current_user)):
    return await workspace_manager.list_workspaces(user["id"])


@router.post("/workspaces")
async def create_workspace(
    name: str, user: dict = Depends(auth.get_current_user)
):
    try:
        return await workspace_manager.create_workspace(user["id"], name)
    except (sqlite3.IntegrityError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, user: dict = Depends(auth.get_current_user)
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if workspace.get("container_id"):
        await container_manager.registry.stop_and_remove_container(
            workspace["container_id"]
        )
    await ws_handler.reset_workspace_state(workspace_id)

    deleted = await workspace_manager.delete_workspace(
        workspace_id, user["id"]
    )
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "deleted"}


# --- Message history endpoints ---


@router.get("/workspaces/{workspace_id}/messages")
async def get_messages(
    workspace_id: str, user: dict = Depends(auth.get_current_user)
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return await user_store.get_messages(workspace_id)


# --- File endpoints ---


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = ".",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return file_service.list_files(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        content = file_service.read_file(user["id"], workspace_id, path)
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
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        deleted = file_service.delete_path(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    return {"path": deleted, "status": "deleted"}


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    old_path: str,
    new_path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        renamed = file_service.rename_path(
            user["id"], workspace_id, old_path, new_path
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
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        resolved = file_service.resolve_path(user["id"], workspace_id, path)
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
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    filename = path if path else file.filename
    if (
        not filename
    ):  # pragma: no cover — FastAPI rejects empty filename at 422 first
        raise HTTPException(status_code=400, detail="No filename provided")

    container_id = workspace.get("container_id")
    if container_id is not None:
        container_manager.registry.record_activity(container_id)

    content = await file.read()
    try:
        saved_path = file_service.write_file(
            user["id"], workspace_id, filename, content
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}


# --- Admin endpoints (require admin role) ---


@router.get("/admin/users")
async def list_users(admin: dict = Depends(auth.require_role("admin"))):
    return await user_store.list_users()


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str, admin: dict = Depends(auth.require_role("admin"))
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = await user_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Stop all containers for this user before deleting
    await container_manager.registry.stop_user_containers(user_id)
    # Archive workspace data before deletion
    await workspace_manager.archive_user_data(user_id, user["email"])
    deleted = await user_store.delete_user(user_id)
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted"}


@router.post("/admin/users/{user_id}/roles/{role_name}")
async def add_user_role(
    user_id: str,
    role_name: str,
    admin: dict = Depends(auth.require_role("admin")),
):
    user = await user_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await user_store.ensure_role(role_name)
    await user_store.assign_role(user_id, role_name)
    return {"status": "assigned", "user_id": user_id, "role": role_name}


@router.delete("/admin/users/{user_id}/roles/{role_name}")
async def remove_user_role(
    user_id: str,
    role_name: str,
    admin: dict = Depends(auth.require_role("admin")),
):
    removed = await user_store.remove_role(user_id, role_name)
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
    user = await user_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if req.email is not None:
        await user_store.update_email(user_id, req.email)
    if req.password is not None:
        password_hash = auth.hash_password(req.password)
        await user_store.update_password(user_id, password_hash)
    return {"status": "updated"}
