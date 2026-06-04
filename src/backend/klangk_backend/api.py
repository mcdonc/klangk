"""API route handlers for Klangk backend."""

import asyncio
import io
import json
import logging
import os
import posixpath
import sqlite3
import subprocess
import tarfile
import tempfile
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
    podman,
    wshandler,
    model,
    workspaces,
)
from .util import derive_hosting_info, resolve_env_secret

logger = logging.getLogger(__name__)

# Maximum upload size for workspace import (bytes).
# Default 500 MB; override via KLANGK_IMPORT_MAX_SIZE (in bytes).
IMPORT_MAX_SIZE = int(
    resolve_env_secret("KLANGK_IMPORT_MAX_SIZE", str(500 * 1024 * 1024))
)

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

    @router.get("/api/test/bridge-tokens/{workspace_id}")
    async def get_bridge_tokens(workspace_id: str):
        """Return all active bridge tokens for a workspace (test only)."""
        tokens = []
        for token, (ws_id, sock) in container.registry._bridge_tokens.items():
            if ws_id == workspace_id:
                # Find the user email for this connection
                email = None
                if sock is not None:
                    conn = wshandler.state.connections.get(sock)
                    if conn:
                        email = conn.user.get("email")
                tokens.append({"token": token, "email": email})
        return tokens


async def _send_email(coro, recipient: str, kind: str = "email") -> None:
    """Await an email-sending coroutine, converting failures to 503."""
    try:
        await coro
    except Exception as e:
        logger.error("Failed to send %s to %s: %s", kind, recipient, e)
        raise HTTPException(
            status_code=503,
            detail=f"Unable to send {kind}. Please try again later.",
        ) from None


# --- Config endpoint ---

SOLIPLEX_URL = resolve_env_secret("SOLIPLEX_URL", "")
LOGIN_BANNER_TITLE = resolve_env_secret("KLANGK_LOGIN_BANNER_TITLE", "")
LOGIN_BANNER = resolve_env_secret("KLANGK_LOGIN_BANNER", "")


@router.get("/api/config")
async def get_config():
    return {
        "soliplex_url": SOLIPLEX_URL,
        "registration_enabled": auth.registration_enabled(),
        "login_banner_title": LOGIN_BANNER_TITLE,
        "login_banner": LOGIN_BANNER,
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
        await _send_email(
            emailsvc.send_verification_email(req.email, verification_url),
            req.email,
            "verification email",
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
    await _send_email(
        emailsvc.send_verification_email(req.email, verification_url),
        req.email,
        "verification email",
    )
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
    await _send_email(
        emailsvc.send_password_reset_email(req.email, reset_url),
        req.email,
        "password reset email",
    )
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
    await _send_email(
        emailsvc.send_verification_email(req.email, url),
        req.email,
        "verification email",
    )
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
    volumes = await podman.list_volumes(
        f"klangk.instance={container.INSTANCE_ID}"
    )
    return [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
        }
        for v in volumes
    ]


class CreateVolumeRequest(BaseModel):
    name: str


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    _user: dict = Depends(auth.get_current_user),
):
    if await podman.inspect_volume(body.name) is not None:
        raise HTTPException(
            status_code=409, detail=f"Volume {body.name!r} already exists"
        )
    info = await podman.create_volume(
        body.name,
        {
            "klangk.managed": "true",
            "klangk.instance": container.INSTANCE_ID,
        },
    )
    return {"name": info["Name"], "created": info.get("CreatedAt", "")}


@router.delete("/volumes/{name}")
async def delete_volume(
    name: str, _user: dict = Depends(auth.get_current_user)
):
    info = await podman.inspect_volume(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    labels = info.get("Labels") or {}
    if labels.get("klangk.instance") != container.INSTANCE_ID:
        raise HTTPException(
            status_code=404,
            detail="Volume not managed by this Klangk instance",
        )
    try:
        await podman.remove_volume(name)
    except podman.PodmanError as e:
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


# --- Workspace export/import endpoints ---


@router.get("/workspaces/{workspace_id}/export")
async def export_workspace(
    workspace_id: str, admin: dict = Depends(auth.require_role("admin"))
):
    """Export a workspace as a .tar.gz archive (admin only).

    The archive contains workspace.json (metadata) and the home
    directory tree under home/.
    """
    workspace = await model.get_workspace(workspace_id, admin["id"])
    if workspace is None:
        # Admin may not own the workspace — look it up without access check.
        workspace = await model.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    home_dir = workspaces.home_path(workspace["user_id"], workspace_id)
    ws_name = workspace["name"]

    metadata = workspaces.workspace_metadata(workspace)

    # Estimate uncompressed size for client progress display.
    estimated_size = 0
    if home_dir.exists():
        try:
            result = subprocess.run(
                ["du", "-sb", str(home_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                estimated_size = int(result.stdout.split()[0])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # fall back to 0

    # Stream the tarball as it's built — no temp file needed.
    # tarfile writes to a queue in a background thread; the async
    # generator reads from the queue and yields chunks to the client.
    import queue
    import threading

    chunk_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=32)

    _WRITE_BUF_SIZE = 256 * 1024  # 256 KB chunks

    class _QueueWriter:
        """File-like object that buffers writes and flushes to a queue."""

        def __init__(self):
            self._buf = bytearray()

        def write(self, data):
            self._buf.extend(data)
            while len(self._buf) >= _WRITE_BUF_SIZE:
                chunk_queue.put(bytes(self._buf[:_WRITE_BUF_SIZE]))
                del self._buf[:_WRITE_BUF_SIZE]
            return len(data)

        def flush(self):
            if self._buf:
                chunk_queue.put(bytes(self._buf))
                self._buf.clear()

        def close(self):
            self.flush()
            chunk_queue.put(None)  # sentinel

    def _build_tar():
        writer = _QueueWriter()
        try:
            with tarfile.open(fileobj=writer, mode="w|gz") as tar:
                # Add workspace.json
                meta_bytes = json.dumps(metadata, indent=2).encode()
                info = tarfile.TarInfo(name="workspace.json")
                info.size = len(meta_bytes)
                tar.addfile(info, io.BytesIO(meta_bytes))

                # Add home directory. Symlinks that resolve outside the
                # home dir are stripped to prevent leaking host files.
                home_resolved = home_dir.resolve()

                def _safe_filter(ti):
                    if ti.issym():
                        target = (home_dir / ti.linkname).resolve()
                        if not target.is_relative_to(home_resolved):
                            return None
                    return ti

                if home_dir.exists():
                    tar.add(str(home_dir), arcname="home", filter=_safe_filter)
        finally:
            writer.close()

    async def _stream():
        loop = asyncio.get_event_loop()
        thread = threading.Thread(target=_build_tar, daemon=True)
        thread.start()
        try:
            while True:
                chunk = await loop.run_in_executor(None, chunk_queue.get)
                if chunk is None:
                    break
                yield chunk
        finally:
            thread.join(timeout=5)

    safe_name = ws_name.replace("/", "_").replace("\\", "_")
    # Rough estimate: gzip typically compresses to ~20% of original
    # for text-heavy home dirs (source code, dotfiles, configs).
    estimated_compressed = max(int(estimated_size * 0.2), 1)
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.tar.gz"',
            "X-Estimated-Size": str(estimated_compressed),
        },
    )


@router.post("/workspaces/import")
async def import_workspace(
    file: UploadFile,
    name: str | None = None,
    user: dict = Depends(auth.get_current_user),
):
    """Import a workspace from a .tar.gz archive.

    Creates a new workspace with metadata from workspace.json and
    extracts the home directory from the archive.
    """
    # Stream upload to a temp file — abort if over the configured limit.
    max_upload = IMPORT_MAX_SIZE
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    total = 0
    try:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_upload:
                raise HTTPException(
                    status_code=413,
                    detail=f"Archive exceeds {max_upload // (1024 * 1024)} MB limit",
                )
            tmp.write(chunk)
        tmp.close()
    except HTTPException:
        os.unlink(tmp.name)
        raise
    except Exception:
        os.unlink(tmp.name)
        raise

    # Extract workspace.json from the archive using tar (fast, no Python parsing).
    ws = None
    try:
        result = subprocess.run(
            ["tar", "xzf", tmp.name, "-O", "workspace.json"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail="Archive missing workspace.json or is corrupt",
            )
        metadata = json.loads(result.stdout)

        ws_name = name or metadata.get("name")
        if not ws_name:
            raise HTTPException(
                status_code=400,
                detail="No workspace name in archive or request",
            )

        image = metadata.get("image")
        if image and image not in container.ALLOWED_IMAGES:
            image = None  # fall back to default

        mounts = metadata.get("mounts")
        if mounts and container.validate_mounts(mounts):
            mounts = None  # invalid mounts, drop them

        # Sanitize env: drop keys that could interfere with
        # container startup (KLANGK_*, LD_*, PATH, etc.)
        raw_env = metadata.get("env")
        if isinstance(raw_env, dict):
            blocked = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PATH"}
            env = {
                k: v
                for k, v in raw_env.items()
                if not k.startswith("KLANGK_") and k not in blocked
            }
        else:
            env = None

        try:
            ws = await workspaces.create_workspace(
                user["id"],
                ws_name,
                image=image,
                default_command=metadata.get("default_command"),
                mounts=mounts,
                env=env,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"A workspace named {ws_name!r} already exists",
            )

        # Extract home directory using tar (much faster than Python tarfile
        # for archives with many members). --strip-components=1 removes
        # the "home/" prefix. Only extract if home/ exists in the archive.
        home_dir = workspaces.home_path(user["id"], ws["id"])
        home_dir.mkdir(parents=True, exist_ok=True)
        check = subprocess.run(
            ["tar", "tzf", tmp.name, "home/"],
            capture_output=True,
            timeout=30,
        )
        if check.returncode == 0:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "tar",
                    "xzf",
                    tmp.name,
                    "--strip-components=1",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "-C",
                    str(home_dir),
                    "home/",
                ],
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                await workspaces.delete_workspace(ws["id"], user["id"])
                raise HTTPException(
                    status_code=400,
                    detail="Failed to extract home directory from archive",
                )

    except HTTPException:
        raise
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        if ws:
            await workspaces.delete_workspace(ws["id"], user["id"])
        raise HTTPException(
            status_code=400, detail="Invalid or corrupt archive"
        )
    finally:
        os.unlink(tmp.name)

    return ws


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

    Each terminal exec session gets a per-connection bridge token
    (injected via the exec environment).  The backend resolves the
    token to the specific browser connection that owns the terminal
    and relays the request over WebSocket.
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
