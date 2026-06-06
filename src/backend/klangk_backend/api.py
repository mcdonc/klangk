"""API route handlers for Klangk backend."""

import asyncio
import io
import json
import logging
import os
import posixpath
import secrets
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid
import zipfile

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from . import (
    acl,
    auth,
    container,
    emailsvc,
    files,
    oidc,
    wshandler,
    model,
    workspaces,
)
from .model import (
    ACTION_ALLOW,
    PRINCIPAL_USER,
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


@router.get("/version")
async def version():
    """Return build version info (version, commit, build timestamp)."""
    version_file = os.environ.get("KLANGK_VERSION_FILE", "")
    if version_file and os.path.isfile(version_file):
        with open(version_file) as f:
            return json.load(f)
    # Dev mode: read from git
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = "unknown"
    return {"version": "dev", "commit": commit, "built_at": None}


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
        "invitations_enabled": auth.invitations_enabled(),
        "login_banner_title": LOGIN_BANNER_TITLE,
        "login_banner": LOGIN_BANNER,
        "oidc_providers": oidc.list_providers(),
        "auth_modes": oidc.auth_modes(),
    }


# --- Auth endpoints ---


@router.post("/auth/register")
async def register(
    req: auth.RegisterRequest,
    request: Request,
):
    if not oidc.password_login_allowed():
        raise HTTPException(
            status_code=403,
            detail="Password registration is disabled",
        )
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
    access_token = auth.create_token(user_id, user["email"])
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
    token = auth.create_token(user_id, user["email"])
    return {"status": "reset", "access_token": token}


@router.post("/auth/login", response_model=auth.TokenResponse)
async def login(req: auth.LoginRequest):
    if not oidc.password_login_allowed():
        raise HTTPException(
            status_code=403, detail="Password login is disabled"
        )
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

    # If the user logged in via OIDC and the provider has logout_redirect
    # enabled, return the IdP logout URL so the frontend can redirect.
    result: dict = {"status": "ok"}
    db_user = await model.get_user_by_email(user["email"])
    if db_user and db_user.get("provider", "local") != "local":
        provider = oidc.get_provider(db_user["provider"])
        if provider:
            hostname, proto, base_path = derive_hosting_info(request.headers)
            post_logout_uri = f"{proto}://{hostname}{base_path}/#/login"
            logout_url = await oidc.build_logout_url(provider, post_logout_uri)
            if logout_url:
                result["oidc_logout_url"] = logout_url
    return result


# --- Invitation endpoints ---


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/accept-invite")
async def accept_invite(req: AcceptInviteRequest):
    """Accept an invitation and create a verified account."""
    result = auth.decode_invitation_token(req.token)
    if result is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired invitation token"
        )
    invitation_id, email = result

    invitation = await model.get_invitation(invitation_id)
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=400, detail="Invitation is no longer valid"
        )

    if len(req.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )

    existing = await model.get_user_by_email(email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="An account with this email already exists"
        )

    password_hash = auth.hash_password(req.password)
    user = await model.create_user(email, password_hash, verified=True)
    await model.mark_invitation_accepted(invitation_id)

    access_token = auth.create_token(user["id"], user["email"])
    return {"status": "accepted", "access_token": access_token}


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

    hostname, proto, base_path = derive_hosting_info(request.headers)
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
    admin: dict = Depends(acl.has_permission("admin")),
):
    """List all invitations (admin only)."""
    return await model.list_invitations()


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
    hostname, proto, base_path = derive_hosting_info(request.headers)
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


# --- OIDC endpoints ---


@router.get("/auth/oidc/{provider_id}/login")
async def oidc_login(
    provider_id: str,
    request: Request,
    cli_redirect: str | None = None,
):
    """Redirect to the OIDC IdP for authentication."""
    if not oidc.oidc_login_allowed():
        raise HTTPException(status_code=404, detail="OIDC not enabled")

    provider = oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    # Validate cli_redirect is localhost only
    if cli_redirect and not cli_redirect.startswith(
        ("http://localhost:", "http://127.0.0.1:")
    ):
        raise HTTPException(
            status_code=400, detail="cli_redirect must be localhost"
        )

    verifier, challenge = oidc.generate_pkce()
    state = secrets.token_urlsafe(32)

    hostname, proto, base_path = derive_hosting_info(request.headers)
    redirect_uri = (
        f"{proto}://{hostname}{base_path}/auth/oidc/{provider_id}/callback"
    )

    auth_url = await oidc.build_auth_url(
        provider, redirect_uri, state, challenge
    )

    response = RedirectResponse(url=auth_url, status_code=302)
    # Store state + verifier + cli_redirect in a cookie
    cookie_value = json.dumps(
        {
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "cli_redirect": cli_redirect,
        }
    )
    response.set_cookie(
        key=f"oidc_{provider_id}",
        value=cookie_value,
        httponly=True,
        max_age=600,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/oidc/{provider_id}/callback")
async def oidc_callback(
    provider_id: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str | None = None,
):
    """Handle the OIDC callback from the IdP."""
    if error:
        raise HTTPException(status_code=400, detail=f"IdP error: {error}")

    provider = oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    # Retrieve and validate state from cookie
    cookie_name = f"oidc_{provider_id}"
    cookie_raw = request.cookies.get(cookie_name)
    if not cookie_raw:
        raise HTTPException(
            status_code=400, detail="Missing OIDC state cookie"
        )

    try:
        cookie_data = json.loads(cookie_raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid OIDC state cookie"
        )

    if cookie_data.get("state") != state:
        raise HTTPException(status_code=400, detail="State mismatch")

    # Exchange code for tokens
    try:
        tokens = await oidc.exchange_code(
            provider,
            code,
            cookie_data["redirect_uri"],
            cookie_data["verifier"],
        )
    except httpx.HTTPStatusError as exc:
        logger.error("OIDC token exchange failed: %s", exc.response.text)
        raise HTTPException(
            status_code=502, detail="Token exchange failed"
        ) from None

    # Validate ID token
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="No ID token in response")

    try:
        claims = await oidc.validate_id_token(
            provider, id_token, access_token=tokens.get("access_token")
        )
    except Exception as exc:
        logger.error("OIDC ID token validation failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="ID token validation failed"
        ) from None

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(
            status_code=502,
            detail="ID token missing sub or email claim",
        )
    auth.validate_email(email)

    # Find or create user
    user = await model.get_user_by_external_id(provider_id, sub)
    if user is None:
        # Check for existing local user with same email
        existing = await model.get_user_by_email(email)
        if existing is not None:
            # Link OIDC identity to existing user
            await model.link_oidc_identity(existing["id"], provider_id, sub)
            user = existing
        else:
            # JIT provisioning
            user = await model.create_user(
                email=email,
                password_hash=None,
                verified=True,
                provider=provider_id,
                external_id=sub,
            )

    # Sync admin group membership from IdP claims
    should_be_admin = oidc.extract_admin_role(provider, claims)
    if should_be_admin is not None:
        admin_group = await model.get_group_by_name("admin")
        if admin_group:
            current_groups = await model.get_user_group_ids(user["id"])
            if should_be_admin and admin_group["id"] not in current_groups:
                await model.add_user_to_group(user["id"], admin_group["id"])
            elif not should_be_admin and admin_group["id"] in current_groups:
                await model.remove_user_from_group(
                    user["id"], admin_group["id"]
                )

    # Issue Klangk JWT
    access_token = auth.create_token(user["id"], email)

    # Clear the state cookie
    cli_redirect = cookie_data.get("cli_redirect")

    if cli_redirect:
        # CLI flow: redirect to the CLI's localhost server with the token
        redirect_url = f"{cli_redirect}?token={access_token}"
    else:
        # Web flow: redirect to the frontend with token in the hash
        hostname, proto, base_path = derive_hosting_info(request.headers)
        redirect_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/oidc-complete?token={access_token}"
        )

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
    return response


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


async def _workspace_resource(request: Request, user: dict) -> str:
    """Resource function for workspace-level permission checks."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


async def _admin_resource(request: Request, user: dict) -> str:  # noqa: ARG001
    """Resource function for admin operations (always checks /admin)."""
    return "/admin"


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
        ws = await workspaces.create_workspace(
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
    # Grant owner full access via ACL
    await model.add_acl_entry(
        f"/workspaces/{ws['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return ws


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
    user: dict = Depends(acl.has_permission("edit", _workspace_resource)),
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
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    updated = await model.update_workspace(
        workspace_id, workspace["user_id"], **fields
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if "default_command" in fields:
        workspaces.write_default_command(
            workspace["user_id"], workspace_id, fields["default_command"]
        )
    return {"status": "updated"}


class DuplicateWorkspaceRequest(BaseModel):
    name: str


@router.post("/workspaces/{workspace_id}/duplicate")
async def duplicate_workspace(
    workspace_id: str,
    body: DuplicateWorkspaceRequest,
    user: dict = Depends(acl.has_permission("create", _workspace_resource)),
):
    source = await model.get_workspace(workspace_id)
    if source is None:  # pragma: no cover — race after ACL check
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        ws = await workspaces.create_workspace(
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
    # Grant owner full access via ACL
    await model.add_acl_entry(
        f"/workspaces/{ws['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return ws


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("delete", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
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

    deleted = await workspaces.delete_workspace(
        workspace_id, workspace["user_id"]
    )
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Clean up ACL entries for this workspace
    await model.delete_acl_entries_for_resource(f"/workspaces/{workspace_id}")
    return {"status": "deleted"}


# --- Workspace export/import endpoints ---


@router.get("/workspaces/{workspace_id}/export")
async def export_workspace(
    workspace_id: str,
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
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


async def _check_workspace_share(request: Request, user: dict) -> str:
    """Resource function for workspace share permission."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


@router.get("/workspaces/{workspace_id}/members")
async def get_workspace_members(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    return await model.get_workspace_members(workspace_id)


class AddMemberRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str,
    body: AddMemberRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(
            status_code=400, detail="Cannot share with yourself"
        )
    # Add ACL entries granting the target user view+terminal on this workspace
    resource = f"/workspaces/{workspace_id}"
    existing = await model.get_acl_entries(resource)
    # Find the next available position
    max_pos = max((e["position"] for e in existing), default=-1)
    await model.add_acl_entry(
        resource,
        max_pos + 1,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await model.add_acl_entry(
        resource,
        max_pos + 2,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await model.add_acl_entry(
        resource,
        max_pos + 3,
        ACTION_ALLOW,
        "files",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    return {
        "status": "shared",
        "user_id": target["id"],
        "email": target["email"],
    }


@router.delete("/workspaces/{workspace_id}/members/{member_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    # Remove all ACL entries for this user on this workspace
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries(resource)
    remaining = [
        e
        for e in entries
        if not (
            e["principal_type"] == PRINCIPAL_USER and e["user_id"] == member_id
        )
    ]
    # Rewrite entries with new positions
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await model.replace_acl_entries(resource, remaining)
    return {"status": "removed"}


# --- Workspace ACL endpoints (for workspace owners/admins) ---


@router.get("/workspaces/{workspace_id}/acl")
async def get_workspace_acl(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Get resolved ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
    return await model.get_acl_entries_resolved(resource)


class WorkspaceAclEntry(BaseModel):
    action: int  # 0=deny, 1=allow
    principal_type: int  # 0=system, 1=user, 2=group
    permission: str
    user_id: str | None = None
    group_id: str | None = None
    system_principal: int | None = None


@router.put("/workspaces/{workspace_id}/acl")
async def replace_workspace_acl(
    workspace_id: str,
    entries: list[WorkspaceAclEntry],
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Replace all ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
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
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return files.list_files(workspace["user_id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
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
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
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
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
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
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
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
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:  # pragma: no cover — race after ACL check
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


def _resolve_bridge_target(body: BrowserDelegateRequest):
    """Resolve a bridge token to (session, target_sock, payload).

    Raises HTTPException (403/502) if the token is invalid, the workspace
    has no session, or the target browser is not subscribed.
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

    if target_sock not in session.browser_subscribers:
        raise HTTPException(
            status_code=502,
            detail="Browser connection not available",
        )
    return session, target_sock, body.model_dump(exclude={"token"})


@router.post("/api/browser-delegate")
async def browser_delegate(body: BrowserDelegateRequest):
    """Bridge endpoint for Pi extensions to delegate actions to the browser.

    Each terminal exec session gets a per-connection bridge token
    (injected via the exec environment).  The backend resolves the
    token to the specific browser connection that owns the terminal
    and relays the request over WebSocket.
    """
    session, target_sock, payload = _resolve_bridge_target(body)
    result = await session.dispatch_browser_request_to(target_sock, payload)

    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/api/browser-delegate/stream")
async def browser_delegate_stream(body: BrowserDelegateRequest):
    """Streaming bridge: relay browser output chunks back as NDJSON.

    For long-running actions (RAG + LLM), the browser pushes incremental
    browser_chunk messages and a terminal browser_response.  Each is streamed
    to the caller immediately, so there is no single bounded round-trip — the
    only limit is the per-chunk idle timeout.
    """
    session, target_sock, payload = _resolve_bridge_target(body)
    return StreamingResponse(
        session.dispatch_browser_request_stream_to(
            target_sock, payload, wshandler.bridge_idle_timeout()
        ),
        media_type="application/x-ndjson",
    )


# --- Admin endpoints (require admin role) ---


@router.get("/admin/users")
async def list_users(admin: dict = Depends(acl.has_permission("admin"))):
    return await model.list_users()


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str


@router.post("/admin/users")
async def admin_create_user(
    req: AdminCreateUserRequest,
    admin: dict = Depends(acl.has_permission("admin")),
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
        password_hash = auth.hash_password(req.password)
        await model.update_password(user_id, password_hash)
    return {"status": "updated"}


# --- Group management endpoints ---


@router.get("/admin/groups")
async def list_groups(
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.list_groups()


class CreateGroupRequest(BaseModel):
    name: str
    description: str | None = None


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


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None


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


class AddGroupMemberRequest(BaseModel):
    user_id: str


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


STATIC_RESOURCES = [
    "/",
    "/workspaces",
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
    "files",
    "share",
    "admin",
    "manage_users",
    "manage_invitations",
    "*",
]


@router.get("/api/my-permissions")
async def my_permissions(
    request: Request,
    resource: str | None = None,
    user: dict = Depends(auth.get_current_user),
):
    """Return the current user's effective permissions.

    If ``resource`` query param is provided, checks permissions for that
    specific resource path (e.g., ``/workspaces/{id}``). Otherwise
    returns permissions for all static resources.
    """
    principals = await acl.get_principals(user["id"])
    groups = await model.get_user_groups(user["id"])
    if resource is not None:
        perms = [
            p
            for p in ALL_PERMISSIONS
            if await acl.check_permission(resource, principals, p)
        ]
        return {
            "user_id": user["id"],
            "email": user["email"],
            "groups": groups,
            "permissions": {resource: perms} if perms else {},
        }
    permissions = {}
    for res in STATIC_RESOURCES:
        perms = [
            p
            for p in ALL_PERMISSIONS
            if await acl.check_permission(res, principals, p)
        ]
        if perms:
            permissions[res] = perms
    return {
        "user_id": user["id"],
        "email": user["email"],
        "groups": groups,
        "permissions": permissions,
    }
